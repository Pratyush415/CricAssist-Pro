"""The web app's API, with a fake AI model (no network)."""

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricassist.assistant.agent import AssistantError
from cricassist.config import Settings
from cricassist.web.app import create_app, render_markdown


class FakeBackend:
    """Calls one stats tool, reports a rate-limit pause, then answers in Markdown."""

    def __init__(self, model, fail=False, gate=None):
        self.model = model
        self.fail = fail
        self.gate = gate  # if set, the answer waits until the test opens it
        self.started = threading.Event()
        self.on_wait = None
        self.on_draft = None
        self.questions = []
        self.seeded = []

    def seed(self, turns):
        self.seeded += turns

    def respond(self, question, execute):
        self.questions.append(question)
        if self.fail:
            raise AssistantError("Gemini's free daily quota for this model is used up.")
        self.started.set()
        if self.gate:
            self.gate.wait(10)
        if self.on_draft:
            self.on_draft("")
        execute("batting_summary", {"player": "Jaiswal", "format": "TEST"})
        if self.on_wait:
            self.on_wait("Gemini free-tier rate limit; waiting 5s...")
        if self.on_draft:
            self.on_draft("Jaiswal made **123**")
        return "Jaiswal made **123** runs.\n\n| Inns | Runs |\n|---|---|\n| 2 | 123 |\n\n<script>alert(1)</script>"


@pytest.fixture
def backends():
    return []


@pytest.fixture
def settings(db_path, tmp_path):
    return Settings(
        db_path=db_path, context_path=Path("unused"), cache_dir=Path("unused"),
        llm_provider="gemini", llm_model="", gemini_api_key="key", anthropic_api_key="",
        chats_path=tmp_path / "chats.json",
    )


@pytest.fixture
def gate():
    """Holds answers from the gemini-3.7-flash fake until the test sets it."""
    return threading.Event()


@pytest.fixture
def make_client(settings, backends, gate):
    """A new app (as after a server restart) over the same chats file."""

    def factory(settings, provider, model):
        backend = FakeBackend(model, fail=model == "gemini-3.1-flash-lite",
                              gate=gate if model == "gemini-3.7-flash" else None)
        backends.append(backend)
        return backend

    return lambda: TestClient(create_app(settings, backend_factory=factory))


@pytest.fixture
def client(make_client):
    return make_client()


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
    assert kinds == ["session", "draft", "tool", "wait", "draft", "answer"]
    drafts = [d["html"] for k, d in events(response) if k == "draft"]
    assert drafts == ["", "<p>Jaiswal made <strong>123</strong></p>"]  # streamed text arrives rendered
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


# --- Saved chats -------------------------------------------------------------


def test_chats_survive_a_restart(make_client, settings, backends):
    first = make_client()
    sid = first.post("/api/sessions", json={}).json()["id"]
    first.post(f"/api/sessions/{sid}/messages", json={"text": "How did Jaiswal do in Tests?"})
    first.post("/api/sessions", json={"model": "gemini-3.5-flash"})
    assert settings.chats_path.is_file()

    restarted = make_client()
    titles = [s["title"] for s in restarted.get("/api/sessions").json()]
    assert titles == ["New chat", "How did Jaiswal do in Tests?"]
    roles = [m["role"] for m in restarted.get(f"/api/sessions/{sid}").json()["messages"]]
    assert roles == ["user", "assistant"]


def test_a_restored_chat_keeps_its_context(make_client, backends):
    first = make_client()
    sid = first.post("/api/sessions", json={}).json()["id"]
    first.post(f"/api/sessions/{sid}/messages", json={"text": "How did Jaiswal do in Tests?"})
    assert backends[0].seeded == []  # a brand-new chat has nothing to restore

    restarted = make_client()
    restarted.post(f"/api/sessions/{sid}/messages", json={"text": "And in T20s?"})
    restored = backends[-1]
    assert [role for role, _ in restored.seeded] == ["user", "assistant"]
    assert restored.seeded[0][1] == "How did Jaiswal do in Tests?"
    assert restored.questions == ["And in T20s?"]


def test_only_answered_questions_since_a_model_switch_are_restored(make_client, backends):
    first = make_client()
    sid = first.post("/api/sessions", json={}).json()["id"]
    first.post(f"/api/sessions/{sid}/messages", json={"text": "Before the switch"})
    first.patch(f"/api/sessions/{sid}", json={"model": "gemini-3.1-flash-lite"})  # this fake model fails
    first.post(f"/api/sessions/{sid}/messages", json={"text": "Fails"})
    first.patch(f"/api/sessions/{sid}", json={"model": "gemini-3.5-flash"})
    first.post(f"/api/sessions/{sid}/messages", json={"text": "After"})

    make_client().post(f"/api/sessions/{sid}/messages", json={"text": "Follow-up"})
    assert [text for _, text in backends[-1].seeded][::2] == ["After"]


def test_deleted_chats_stay_deleted(make_client):
    first = make_client()
    sid = first.post("/api/sessions", json={}).json()["id"]
    first.delete(f"/api/sessions/{sid}")
    assert make_client().get("/api/sessions").json() == []


def test_a_damaged_chats_file_is_set_aside(make_client, settings):
    settings.chats_path.write_text("{not json", "utf-8")
    client = make_client()
    assert client.get("/api/sessions").json() == []
    assert settings.chats_path.with_name("chats.json.damaged").is_file()


# --- Several chats at once ---------------------------------------------------


def test_other_chats_work_while_one_is_answering(client, backends, gate, settings):
    slow = client.post("/api/sessions", json={"model": "gemini-3.7-flash"}).json()["id"]
    fast = client.post("/api/sessions", json={}).json()["id"]

    result = {}
    worker = threading.Thread(target=lambda: result.update(
        response=client.post(f"/api/sessions/{slow}/messages", json={"text": "Slow question"})))
    worker.start()
    for _ in range(100):  # the request creates the model's backend on another thread
        slow_backend = next((b for b in backends if b.model == "gemini-3.7-flash"), None)
        if slow_backend:
            break
        threading.Event().wait(0.05)
    assert slow_backend and slow_backend.started.wait(5)

    running = {s["id"]: s["running"] for s in client.get("/api/sessions").json()}
    assert running == {slow: True, fast: False}
    assert client.get(f"/api/sessions/{slow}").json()["running"] is True
    # The busy chat refuses a second question; another chat answers meanwhile.
    assert client.post(f"/api/sessions/{slow}/messages", json={"text": "Again"}).status_code == 409
    other = client.post(f"/api/sessions/{fast}/messages", json={"text": "Fast question"})
    assert events(other)[-1][0] == "answer"
    assert "running" not in settings.chats_path.read_text("utf-8")  # never saved

    gate.set()
    worker.join(10)
    assert events(result["response"])[-1][0] == "answer"
    assert client.get(f"/api/sessions/{slow}").json()["running"] is False
