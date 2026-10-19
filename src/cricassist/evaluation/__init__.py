"""Evaluation: a fixed set of journalist questions with verified answers.

Each answer is graded on two things:

1. Correct: it contains the expected facts (and, for questions the data cannot
   answer, says so without inventing anything).
2. Sourced: every number in it appears in the question, a tool's arguments
   or a tool's results. Anything else is reported as unsourced, since it
   could only have come from the model's memory or its own arithmetic.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib import resources

# Small numbers turn up as list positions, "top 3", "1st innings" and so on.
SMALL_NUMBERS = {0, 1, 2, 3}

# A number, not part of a word ("T20") and not a plural label ("50s", "4s", "1990s").
_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w])")


def load_questions() -> list[dict]:
    text = resources.files("cricassist").joinpath("evaluation", "questions.json").read_text("utf-8")
    return json.loads(text)["questions"]


def numbers_in(text: str) -> set[float]:
    """Every number in a piece of text, as a float ("1,027" -> 1027.0, "60.70" -> 60.7)."""
    return {float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(text)}


def unsourced_numbers(answer: str, sources: list[str]) -> list[float]:
    """Numbers in the answer that appear in none of the sources."""
    known = set()
    for s in sources:
        known |= numbers_in(s)
    missing = []
    for n in sorted(numbers_in(answer)):
        if n in SMALL_NUMBERS:
            continue
        if not any(abs(n - k) < 0.006 for k in known):
            missing.append(n)
    return missing


@dataclass
class Grade:
    id: str
    type: str
    question: str
    expected: str
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # required facts not found
    forbidden: list[str] = field(default_factory=list)  # forbidden patterns found
    unsourced: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.error is None and not self.missing and not self.forbidden

    @property
    def sourced(self) -> bool:
        return self.error is None and not self.unsourced

    def to_dict(self) -> dict:
        return {**asdict(self), "correct": self.correct, "sourced": self.sourced}


def grade_answer(q: dict, answer: str, tool_calls: list[dict], tool_results: list[str]) -> Grade:
    grade = Grade(q["id"], q["type"], q["question"], q["expected"], answer=answer, tool_calls=tool_calls)
    for alternatives in q.get("must_include", []):
        if not any(re.search(p, answer, re.I | re.S) for p in alternatives):
            grade.missing.append(" | ".join(alternatives))
    for pattern in q.get("must_not_include", []):
        if re.search(pattern, answer, re.I | re.S):
            grade.forbidden.append(pattern)
    sources = [q["question"], *tool_results, *(json.dumps(c["args"]) for c in tool_calls)]
    grade.unsourced = unsourced_numbers(answer, sources)
    return grade


def run_question(q: dict, make_assistant: Callable) -> Grade:
    """Ask one question in a fresh conversation and grade the answer."""
    try:
        answer = make_assistant().ask(q["question"])
    except Exception as e:  # a provider error (quota, network) fails this question, not the run
        return Grade(q["id"], q["type"], q["question"], q["expected"], error=f"{type(e).__name__}: {e}")
    calls = [{"name": c.name, "args": c.args, "is_error": c.is_error} for c in answer.tool_calls]
    return grade_answer(q, answer.text, calls, [c.result for c in answer.tool_calls])


def run_evaluation(
    make_assistant: Callable,
    questions: list[dict] | None = None,
    previous: dict[str, dict] | None = None,
    pause: float = 0.0,
    log: Callable[[str], None] = print,
    on_result: Callable[[Grade], None] | None = None,
) -> list[Grade]:
    """Run each question. Questions already answered without error in
    `previous` (from an earlier run) are skipped, so a run cut short by a
    quota can be resumed."""
    questions = questions if questions is not None else load_questions()
    previous = previous or {}
    grades = []
    for i, q in enumerate(questions, 1):
        if q["id"] in previous and not previous[q["id"]].get("error"):
            log(f"[{i}/{len(questions)}] {q['id']}: kept from earlier run")
            continue
        log(f"[{i}/{len(questions)}] {q['id']} ...")
        grade = run_question(q, make_assistant)
        grades.append(grade)
        if on_result:
            on_result(grade)
        if pause and i < len(questions):
            time.sleep(pause)
    return grades


def summarise(results: dict[str, dict], questions: list[dict]) -> str:
    """A plain-text table of results, in question order."""
    lines = [f"{'question':<28} {'type':<14} {'correct':<8} {'sourced':<8} notes"]
    correct = sourced = answered = 0
    for q in questions:
        r = results.get(q["id"])
        if r is None:
            lines.append(f"{q['id']:<28} {q['type']:<14} {'-':<8} {'-':<8} not run")
            continue
        notes = r.get("error") or "; ".join(
            [f"missing {m}" for m in r["missing"]] + [f"forbidden {f}" for f in r["forbidden"]]
            + ([f"unsourced {r['unsourced']}"] if r["unsourced"] else [])
        )
        if not r.get("error"):
            answered += 1
            correct += r["correct"]
            sourced += r["sourced"]
        lines.append(f"{q['id']:<28} {q['type']:<14} {'yes' if r['correct'] else 'NO':<8} {'yes' if r['sourced'] else 'NO':<8} {notes}")
    lines.append(f"\n{correct} of {len(questions)} correct, {sourced} of {len(questions)} fully sourced "
                 f"({answered} answered).")
    return "\n".join(lines)
