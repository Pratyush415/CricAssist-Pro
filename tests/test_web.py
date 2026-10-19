"""The web app's API, with a fake AI model (no network)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricassist.assistant.agent import AssistantError
from cricassist.config import Settings
from cricassist.web.app import create_app, render_markdown


class FakeBackend:
    """Calls one stats tool, reports a rate-limit pause, then answers in Markdown."""

    def __init__(self, model, fail=False):
        self.model = model
        self.fail = fail
        self.on_wait = None
        self.questions = []

    def respond(self, question, execute):
        self.questions.append(question)
        if self.fail:
            raise AssistantError("Gemini's free daily quota for this model is used up.")
        execute("batting_summary", {"player": "Jaiswal", "format": "TEST"})
        if self.on_wait:
            self.on_wait("Gemini free-tier rate limit; waiting 5s...")
        return "Jaiswal made **123** runs.\n\n| Inns | Runs |\n|---|---|\n| 2 | 123 |\n\n<script>alert(1)</script>"


@pytest.fixture
def backends():
    return []


@pytest.fixture
def client(db_path, backends):
    settings = Settings(
        db_path=db_path, context_path=Path("unused"), cache_dir=Path("unused"),
        llm_provider="gemini", llm_model="", gemini_api_key="key", anthropic_api_key="",
    )

    def factory(settings, provider, model):
        backend = FakeBackend(model, fail=model == "gemini-3.1-flash-lite")
        backends.append(backend)
        return backend

    return TestClient(create_app(settings, backend_factory=factory))


def events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_page_and_static_files(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="splash"' in page.text and 'symbol id="bat"' in page.text and 'symbol id="ball"' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_models(client):
    data = client.get("/api/models").json()
    assert data["default"] == "gemini-3.8-flash"
    available = {m["id"]: m["available"] for m in data["models"]}
    assert available["gemini-3.8-flash"] is True
    assert available["claude-opus-5-5"] is False  # no Anthropic key in these settings


def test_sessions_lifecycle(client):
    assert client.get("/api/sessions").json() == []
    first = client.post("/api/sessions", json={}).json()
    second = client.post("/api/sessions", json={"model": "gemini-3.5-flash"}).json()
    assert first["model"] == "gemini-3.8-flash" and second["model"] == "gemini-3.5-flash"
    assert [s["id"] for s in client.get("/api/sessions").json()] == [second["id"], first["id"]]  # newest first
    assert client.delete(f"/api/sessions/{first['id']}").status_code == 200
    assert client.get(f"/api/sessions/{first['id']}").status_code == 404
    assert client.post("/api/sessions", json={"model": "gpt-9"}).status_code == 400


def test_asking_streams_tools_waits_and_the_answer(client, backends):
    sid = client.post("/api/sessions", json={}).json()["id"]
    response = client.post(f"/api/sessions/{sid}/messages", json={"text": "How did Jaiswal do in Tests?"})
    kinds = [k for k, _ in events(response)]
    assert kinds == ["session", "tool", "wait", "answer"]
    answer = events(response)[-1][1]
    assert answer["tool_calls"][0]["name"] == "batting_summary"
    assert "<strong>123</strong>" in answer["html"] and "<table>" in answer["html"]
    assert "<script>" not in answer["html"]  # model HTML is shown as text, never run

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["title"] == "How did Jaiswal do in Tests?"
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]


def test_follow_up_questions_share_one_conversation(client, backends):
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "First"})
    client.post(f"/api/sessions/{sid}/messages", json={"text": "And second?"})
    assert len(backends) == 1 and backends[0].questions == ["First", "And second?"]


def test_switching_model_starts_a_new_conversation(client, backends):
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "First"})
    assert client.patch(f"/api/sessions/{sid}", json={"model": "gemini-3.5-flash"}).json()["model"] == "gemini-3.5-flash"
    client.post(f"/api/sessions/{sid}/messages", json={"text": "Second"})
    assert [b.model for b in backends] == ["gemini-3.8-flash", "gemini-3.5-flash"]
    roles = [m["role"] for m in client.get(f"/api/sessions/{sid}").json()["messages"]]
    assert roles == ["user", "assistant", "notice", "user", "assistant"]


def test_provider_errors_show_in_the_chat(client):
    sid = client.post("/api/sessions", json={"model": "gemini-3.1-flash-lite"}).json()["id"]
    response = client.post(f"/api/sessions/{sid}/messages", json={"text": "Anything"})
    kind, data = events(response)[-1]
    assert kind == "error" and "quota" in data["text"]
    assert client.get(f"/api/sessions/{sid}").json()["messages"][-1]["role"] == "error"


def test_bad_requests(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "   "}).status_code == 400
    assert client.post("/api/sessions/nope/messages", json={"text": "hi"}).status_code == 404


def test_render_markdown_escapes_html():
    html = render_markdown("**bold** <b>raw</b> & more")
    assert "<strong>bold</strong>" in html and "&lt;b&gt;raw" in html and "&amp; more" in html
    assert "<b>" not in html
