"""The assistant's tools and conversation loop, with fake AI models (no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from cricassist.assistant import agent
from cricassist.assistant.agent import Assistant, AssistantError, ClaudeBackend, GeminiBackend, make_backend
from cricassist.assistant.prompts import SYSTEM_PROMPT
from cricassist.assistant.tools import FILTER_PROPERTIES, TOOLS, TOOLS_BY_NAME, run_tool
from cricassist.config import Settings, load_dotenv

# --- Tools -------------------------------------------------------------------

SAMPLE_ARGS = {
    "dataset_info": {},
    "find_players": {"query": "Jaiswal"},
    "batting_summary": {"player": "Jaiswal", "format": "TEST"},
    "bowling_summary": {"player": "Bumrah", "format": "TEST"},
    "recent_innings": {"player": "Bumrah", "role": "bowling", "last": 3},
    "player_by_year": {"player": "Jaiswal", "role": "batting", "by_format": True},
    "batter_vs_bowler": {"batter": "Jaiswal", "bowler": "Hasan Mahmud"},
    "batter_vs_bowling_style": {"batter": "Jaiswal", "style": "pace"},
    "player_phase_stats": {"player": "Starc", "role": "bowling"},
    "delivery_breakdown": {"player": "Jaiswal", "by": "length"},
    "team_head_to_head": {"team": "DC", "other_team": "RR"},
    "team_results": {"team": "India"},
    "team_phase_stats": {"team": "RR", "role": "batting"},
    "venue_summary": {"venue": "Kanpur", "format": "TEST"},
    "leaders": {"stat": "runs", "limit": 3},
    "best_innings": {"kind": "team_highest", "limit": 3},
    "search_written_context": {"query": "2023 World Cup final"},
    "match_scorecard": {"match_id": 1439892},
}


def test_every_tool_has_a_sample():
    assert set(SAMPLE_ARGS) == set(TOOLS_BY_NAME)


@pytest.mark.parametrize("name", sorted(SAMPLE_ARGS))
def test_every_tool_runs(db, name, monkeypatch):
    from cricassist.assistant import tools

    monkeypatch.setattr(tools, "_get_context_store", lambda: None)  # no vector database in these tests
    result, is_error = run_tool(db, name, SAMPLE_ARGS[name])
    assert not is_error, result
    assert isinstance(json.loads(result), dict)


def test_tool_schemas_are_well_formed():
    assert len(TOOLS_BY_NAME) == len(TOOLS)
    for tool in TOOLS:
        schema = tool.parameters
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"]), tool.name
        assert tool.description


def test_filters_reach_the_query(db):
    tests_only = json.loads(run_tool(db, "batting_summary", {"player": "Jaiswal", "format": "TEST"})[0])
    everything = json.loads(run_tool(db, "batting_summary", {"player": "Jaiswal"})[0])
    assert tests_only["innings"] == 2 and everything["innings"] == 3
    assert set(FILTER_PROPERTIES) >= {"format", "season", "opposition", "venue"}


def test_tool_errors_come_back_as_results(db):
    result, is_error = run_tool(db, "batting_summary", {"player": "Hasan"})
    assert is_error and len(json.loads(result)["candidates"]) > 1
    result, is_error = run_tool(db, "team_results", {"team": "Afghanistan"})
    assert is_error and "no Afghanistan" in json.loads(result)["error"]
    assert run_tool(db, "no_such_tool", {})[1]
    assert run_tool(db, "leaders", {"stat": "catches"})[1]


def test_prompt_keeps_numbers_to_tool_results():
    assert "must come from a tool result" in SYSTEM_PROMPT
    assert "Do not add up, count or average across rows" in SYSTEM_PROMPT
    assert "rankings are not in the data" in SYSTEM_PROMPT
    assert "Only answer questions about cricket" in SYSTEM_PROMPT
    assert "You cannot open links" in SYSTEM_PROMPT


# --- Gemini loop -------------------------------------------------------------


class FakeGeminiModels:
    """Streams scripted replies (each a list of chunks) and records what was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def generate_content_stream(self, model, contents, config):
        self.sent.append(list(contents))
        return iter(self.replies.pop(0))


def gemini_chunk(parts):
    from google.genai import types

    return NS(candidates=[NS(content=types.Content(role="model", parts=parts), finish_reason="STOP")])


def gemini_reply(calls=None, text=None):
    """A streamed reply: tool calls in one chunk, then the text split over two chunks."""
    from google.genai import types

    chunks = []
    if calls:
        chunks.append(gemini_chunk([
            types.Part(function_call=types.FunctionCall(id=f"c{i}", name=n, args=a)) for i, (n, a) in enumerate(calls)
        ]))
    if text:
        half = len(text) // 2
        chunks += [gemini_chunk([types.Part(text=text[:half])]), gemini_chunk([types.Part(text=text[half:])])]
    return chunks


def test_gemini_loop_runs_tools_and_returns_the_answer(db):
    backend = GeminiBackend("key", "gemini-test", SYSTEM_PROMPT, TOOLS)
    backend.client = NS(models=FakeGeminiModels([
        gemini_reply(calls=[("batting_summary", {"player": "Jaiswal", "format": "TEST"}), ("find_players", {"query": "Hasan"})]),
        gemini_reply(text="Jaiswal made 123 runs."),
    ]))
    drafts = []
    backend.on_draft = drafts.append
    answer = Assistant(db, backend).ask("How did Jaiswal do?")
    assert answer.text == "Jaiswal made 123 runs."
    assert [c.name for c in answer.tool_calls] == ["batting_summary", "find_players"]
    # The answer streamed in: cleared at each turn, then growing to the full text.
    assert drafts == ["", "", "Jaiswal mad", "Jaiswal made 123 runs."]
    # Both results went back in one turn, matched to their calls.
    returned = backend.client.models.sent[1][-1]
    assert [p.function_response.id for p in returned.parts] == ["c0", "c1"]
    assert returned.parts[0].function_response.response["result"]["runs"] == 123


def test_gemini_loop_stops_if_the_model_never_answers(db, monkeypatch):
    monkeypatch.setattr(agent, "MAX_TOOL_ROUNDS", 2)
    backend = GeminiBackend("key", "gemini-test", SYSTEM_PROMPT, TOOLS)
    backend.client = NS(models=FakeGeminiModels([gemini_reply(calls=[("dataset_info", {})])] * 2))
    assert "Stopped" in Assistant(db, backend).ask("?").text


def test_gemini_daily_quota_is_reported(db):
    from google.genai import errors

    backend = GeminiBackend("key", "gemini-test", SYSTEM_PROMPT, TOOLS)

    def over_quota(**kwargs):
        raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        ]}})

    backend.client = NS(models=NS(generate_content_stream=over_quota))
    with pytest.raises(AssistantError, match="daily quota"):
        Assistant(db, backend).ask("?")


def test_gemini_overload_suggests_another_model(db, monkeypatch):
    from google.genai import errors

    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)  # skip the real waits between retries
    backend = GeminiBackend("key", "gemini-test", SYSTEM_PROMPT, TOOLS)
    calls = []

    def overloaded(**kwargs):
        calls.append(1)
        raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})

    backend.client = NS(models=NS(generate_content_stream=overloaded))
    with pytest.raises(AssistantError, match="overloaded.*model picker"):
        Assistant(db, backend).ask("?")
    assert len(calls) == 4  # tried, then retried three times


# --- Claude loop -------------------------------------------------------------


class FakeClaudeStream:
    def __init__(self, reply):
        self.reply = reply
        self.text_stream = iter([b.text for b in reply.content if b.type == "text"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.reply


class FakeClaudeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def stream(self, **kwargs):
        self.sent.append({**kwargs, "messages": list(kwargs["messages"])})  # as sent, before later turns
        return FakeClaudeStream(self.responses.pop(0))


def claude_reply(stop_reason, blocks):
    return NS(stop_reason=stop_reason, content=blocks)


def test_claude_loop_runs_tools_and_returns_the_answer(db):
    backend = ClaudeBackend("key", "claude-opus-5-5", SYSTEM_PROMPT, TOOLS)
    messages = FakeClaudeMessages([
        claude_reply("tool_use", [
            NS(type="text", text="Checking."),
            NS(type="tool_use", id="t1", name="batting_summary", input={"player": "Jaiswal", "format": "TEST"}),
            NS(type="tool_use", id="t2", name="team_results", input={"team": "Afghanistan"}),
        ]),
        claude_reply("end_turn", [NS(type="text", text="Jaiswal made 123 runs.")]),
    ])
    backend.client = NS(beta=NS(messages=messages))
    drafts = []
    backend.on_draft = drafts.append
    answer = Assistant(db, backend).ask("How did Jaiswal do?")
    assert answer.text == "Jaiswal made 123 runs."
    assert drafts == ["", "Checking.", "", "Jaiswal made 123 runs."]
    first = messages.sent[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] and first["output_config"] == {"effort": "medium"}
    assert first["tools"][0]["input_schema"]["type"] == "object"
    # Both results in one user message, the failed one flagged.
    results = messages.sent[1]["messages"][-1]["content"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [("t1", False), ("t2", True)]


def test_claude_refusal_and_cut_off(db):
    backend = ClaudeBackend("key", "claude-haiku-5-5", SYSTEM_PROMPT, TOOLS)
    backend.client = NS(beta=NS(messages=FakeClaudeMessages([
        claude_reply("refusal", []),
        claude_reply("max_tokens", [NS(type="text", text="Partial")]),
    ])))
    assistant = Assistant(db, backend)
    assert "declined" in assistant.ask("?").text
    cut_off = assistant.ask("?").text
    assert cut_off.startswith("Partial") and "cut off" in cut_off
    assert "fallbacks" not in backend.client.beta.messages.sent[0]  # only for models that support it


# --- Settings ----------------------------------------------------------------


def settings(**overrides):
    base = dict(db_path=None, context_path=None, cache_dir=None, llm_provider="gemini", llm_model="",
                gemini_api_key="", anthropic_api_key="")
    return Settings(**{**base, **overrides})


def test_make_backend_needs_the_right_key():
    with pytest.raises(AssistantError, match="GEMINI_API_KEY"):
        make_backend(settings())
    with pytest.raises(AssistantError, match="ANTHROPIC_API_KEY"):
        make_backend(settings(gemini_api_key="x"), provider="claude")
    with pytest.raises(AssistantError, match="Unknown provider"):
        make_backend(settings(), provider="gpt")
    backend = make_backend(settings(llm_provider="claude", anthropic_api_key="x"))
    assert isinstance(backend, ClaudeBackend) and backend.model == "claude-opus-5-5"


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nCRICASSIST_TEST_A="quoted"\nCRICASSIST_TEST_B=from-file\n\nnot a line\n', "utf-8")
    monkeypatch.delenv("CRICASSIST_TEST_A", raising=False)
    monkeypatch.setenv("CRICASSIST_TEST_B", "from-shell")
    load_dotenv(env)
    import os

    assert os.environ["CRICASSIST_TEST_A"] == "quoted"
    assert os.environ["CRICASSIST_TEST_B"] == "from-shell"  # the shell wins
    monkeypatch.delenv("CRICASSIST_TEST_A")
