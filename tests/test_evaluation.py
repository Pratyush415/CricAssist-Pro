import re

import pytest

from cricassist.assistant.agent import Assistant
from cricassist.evaluation import grade_answer, load_questions, numbers_in, run_evaluation, summarise, unsourced_numbers

QUESTIONS = {q["id"]: q for q in load_questions()}


def test_question_set_shape():
    assert len(QUESTIONS) == 15
    types = [q["type"] for q in QUESTIONS.values()]
    assert {"stats", "context", "combined", "cannot_answer"} <= set(types)


@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_patterns_are_valid(qid):
    q = QUESTIONS[qid]
    for alternatives in q.get("must_include", []):
        for p in alternatives:
            re.compile(p)
    for p in q.get("must_not_include", []):
        re.compile(p)
    if q["type"] == "stats":
        # The expected answer itself must pass the checks.
        assert grade_answer(q, q["expected"], [], []).correct, q["expected"]


def test_numbers_in():
    assert numbers_in("He made 9,230 runs at 46.85, SR 60.70 in 2023-11-19.") == {9230.0, 46.85, 60.7, 2023.0, 11.0, 19.0}
    # Digits inside words (T20) and plural labels (4s, 50s / 100s) are not numbers.
    assert numbers_in("T20 and 4s; 50s / 100s: 4 / 0") == {4.0, 0.0}


def test_unsourced_numbers():
    tool_result = '{"runs": 296, "sixes": 8, "average": 98.67, "date": "2022-10-23"}'
    answer = "Kohli hit 8 sixes and made 296 runs at 98.67 on 23 October 2022, in 7 matches with 1 duck."
    assert unsourced_numbers(answer, [tool_result]) == [7.0]  # 1 is ignored as a small number


def test_grade_answer():
    q = QUESTIONS["kohli_sixes_2022_t20wc"]
    good = grade_answer(q, "He hit 8 sixes.", [], ['{"sixes": 8}'])
    assert good.correct and good.sourced
    wrong = grade_answer(q, "He hit 9 sixes.", [], ['{"sixes": 8}'])
    assert not wrong.correct and wrong.missing and wrong.unsourced == [9.0]


def test_cannot_answer_question_rejects_an_invented_duration():
    q = QUESTIONS["steyn_ranking"]
    honest = "ICC rankings are not available in this data, so I cannot say. His record by year is below."
    invented = "Steyn was ranked No. 1 for 2356 days. Rankings are not tracked here, though."
    assert grade_answer(q, honest, [], []).correct
    assert not grade_answer(q, invented, [], []).correct


def test_context_question_rejects_a_made_up_quote():
    q = QUESTIONS["kohli_quote_pakistan_2022"]
    honest = "No written context is available, so I can't quote him. He made 82* (53)."
    invented = 'No written context is available, but he said "this is the best innings I have ever played".'
    assert grade_answer(q, honest, [], []).correct
    assert not grade_answer(q, invented, [], []).correct


class ScriptedBackend:
    """Calls one tool, then answers with fixed text."""

    model = "scripted"

    def __init__(self, text, tool=None):
        self.text, self.tool = text, tool

    def respond(self, question, execute):
        if self.tool:
            execute(*self.tool)
        return self.text


def test_run_evaluation_grades_and_resumes(db):
    questions = [QUESTIONS["kohli_sixes_2022_t20wc"], QUESTIONS["lowest_test_total"]]
    backend = ScriptedBackend("West Indies were out for 27, the lowest; Kohli hit 8 sixes.",
                              tool=("best_innings", {"kind": "team_lowest", "limit": 1}))
    grades = run_evaluation(lambda: Assistant(db, backend), questions, log=lambda m: None)
    assert [g.id for g in grades] == ["kohli_sixes_2022_t20wc", "lowest_test_total"]
    assert all(g.correct for g in grades)
    assert grades[0].tool_calls[0]["name"] == "best_innings"
    # 27 and 8 do not appear in the tiny test database's results, so they count as unsourced.
    assert grades[0].unsourced

    previous = {g.id: g.to_dict() for g in grades}
    assert run_evaluation(lambda: Assistant(db, backend), questions, previous=previous, log=lambda m: None) == []
    table = summarise(previous, questions)
    assert "2 of 2 correct" in table


def test_provider_errors_fail_one_question(db):
    class Broken:
        model = "broken"

        def respond(self, question, execute):
            raise RuntimeError("quota")

    grades = run_evaluation(lambda: Assistant(db, Broken()), [QUESTIONS["highest_ipl_score"]], log=lambda m: None)
    assert grades[0].error and not grades[0].correct


def test_numbers_from_written_context_are_not_a_source(db):
    """A figure the model copies from a Wikipedia passage is unsourced: figures must come from stats tools."""

    class PassageCopier:
        model = "copier"

        def respond(self, question, execute):
            execute("search_written_context", {"query": "anything"})
            return "Wikipedia says he took 99 wickets (https://en.wikipedia.org/wiki/X)."

    from cricassist.assistant import tools

    passage = {"passages": [{"text": "He took 99 wickets.", "url": "https://en.wikipedia.org/wiki/X",
                             "title": "X", "date": None, "kind": "player_profile", "source": "Wikipedia",
                             "relevance": 0.9}], "note": ""}
    original = tools.TOOLS_BY_NAME["search_written_context"]
    tools.TOOLS_BY_NAME["search_written_context"] = original.__class__(
        original.name, original.description, original.parameters, lambda con, a: passage)
    try:
        grade = run_evaluation(lambda: Assistant(db, PassageCopier()), [QUESTIONS["bumrah_bowling_action"]],
                               log=lambda m: None)[0]
    finally:
        tools.TOOLS_BY_NAME["search_written_context"] = original
    assert grade.unsourced == [99.0]
