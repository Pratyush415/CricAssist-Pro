"""FastAPI server for the web app.

Chat sessions are saved to a JSON file (settings.chats_path) after every
change and loaded when the server starts. Each session has its own
conversation with the model (so follow-up questions work) and its own
database cursor; a chat restored from the file is given its earlier questions
and answers as text the first time it is continued. Asking a question streams Server-Sent Events: one per tool
call and rate-limit pause while the assistant works, then the answer.
"""

import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cricassist import __version__
from cricassist.assistant.agent import Assistant, AssistantError, ToolCall, make_backend
from cricassist.config import DEFAULT_MODELS, load_settings
from cricassist.stats.db import connect

# Models offered in the picker. Gemini's free tier has a separate daily quota per model.
GEMINI_MODELS = [
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gemini-3.7-flash", "Gemini 3.7 Flash"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
]
CLAUDE_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
]
TITLE_LENGTH = 48
DRAFT_INTERVAL = 0.08  # seconds between streamed draft updates sent to the browser


def render_markdown(text: str) -> str:
    """The model's Markdown as HTML. Raw HTML from the model is shown as text, not run."""
    safe = text.replace("&", "&amp;").replace("<", "&lt;")
    return markdown.markdown(safe, extensions=["tables", "fenced_code", "sane_lists"])


def _title(question: str) -> str:
    question = re.sub(r"\s+", " ", question).strip()
    return question if len(question) <= TITLE_LENGTH else question[: TITLE_LENGTH - 1].rstrip() + "…"


@dataclass
class Session:
    id: str
    model: str
    title: str = "New chat"
    created: float = field(default_factory=time.time)
    messages: list[dict] = field(default_factory=list)
    assistant: Assistant | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "model": self.model, "created": self.created,
                "running": self.lock.locked()}  # still answering a question

    def to_dict(self) -> dict:
        """What is saved to the chats file (never `running`: nothing runs after a restart)."""
        return {"id": self.id, "title": self.title, "model": self.model, "created": self.created,
                "messages": self.messages}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(id=d["id"], model=d["model"], title=d.get("title", "New chat"),
                   created=d.get("created", time.time()), messages=d.get("messages", []))

    def earlier_turns(self) -> list[tuple[str, str]]:
        """Answered questions since the last model switch, as (role, text) pairs."""
        messages = self.messages
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "notice":
                messages = messages[i + 1:]
                break
        turns = []
        for question, answer in zip(messages, messages[1:]):
            if question["role"] == "user" and answer["role"] == "assistant":
                turns += [("user", question["text"]), ("assistant", answer["text"])]
        return turns


class ChatFile:
    """All chat sessions in one JSON file, rewritten whole on every change."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict[str, Session]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            # Keep a damaged file for inspection rather than overwrite it.
            self.path.replace(self.path.with_name(self.path.name + ".damaged"))
            return {}
        return {d["id"]: Session.from_dict(d) for d in data.get("sessions", [])}

    def save(self, sessions: dict[str, Session]) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            payload = {"version": 1, "sessions": [s.to_dict() for s in sessions.values()]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), "utf-8")
            os.replace(tmp, self.path)  # a crash mid-write leaves the old file intact


class NewSession(BaseModel):
    model: str | None = None


class ModelChange(BaseModel):
    model: str


class Question(BaseModel):
    text: str


def _call_dict(call: ToolCall) -> dict:
    return {"name": call.name, "args": call.args, "is_error": call.is_error}


def create_app(settings=None, backend_factory=make_backend) -> FastAPI:
    """The app. `backend_factory(settings, provider, model)` makes a model backend; tests pass a fake one."""
    settings = settings or load_settings()
    con = connect(settings.db_path)  # fails early, with a clear message, if the database is missing
    chat_file = ChatFile(settings.chats_path)
    sessions: dict[str, Session] = chat_file.load()
    sessions_lock = threading.Lock()

    def save() -> None:
        with sessions_lock:
            snapshot = dict(sessions)
        chat_file.save(snapshot)

    def models() -> list[dict]:
        gemini_ready = bool(settings.gemini_api_key)
        claude_ready = bool(settings.anthropic_api_key)
        listed = [{"id": m, "label": label, "provider": "gemini", "available": gemini_ready} for m, label in GEMINI_MODELS]
        listed += [{"id": m, "label": label, "provider": "claude", "available": claude_ready} for m, label in CLAUDE_MODELS]
        configured = settings.llm_model
        if configured and configured not in {m["id"] for m in listed}:
            listed.insert(0, {"id": configured, "label": configured, "provider": settings.llm_provider, "available": True})
        return listed

    def provider_of(model: str) -> str:
        for m in models():
            if m["id"] == model:
                return m["provider"]
        raise HTTPException(400, f"Unknown model {model!r}")

    def default_model() -> str:
        return settings.llm_model or DEFAULT_MODELS.get(settings.llm_provider, GEMINI_MODELS[0][0])

    def get_session(session_id: str) -> Session:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "No such chat")
        return session

    def assistant_for(session: Session) -> Assistant:
        if session.assistant is None:
            backend = backend_factory(settings, provider_of(session.model), session.model)
            turns = session.earlier_turns()
            if turns and hasattr(backend, "seed"):
                backend.seed(turns)  # a chat restored from the file picks up where it left off
            session.assistant = Assistant(con.cursor(), backend)
        return session.assistant

    app = FastAPI(title="CricAssist", version=__version__)
    static = resources.files("cricassist").joinpath("web", "static")
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return static.joinpath("index.html").read_text("utf-8")

    @app.get("/api/models")
    def list_models():
        return {"default": default_model(), "models": models()}

    @app.get("/api/sessions")
    def list_sessions():
        return [s.summary() for s in sorted(sessions.values(), key=lambda s: s.created, reverse=True)]

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        model = body.model or default_model()
        provider_of(model)
        session = Session(id=uuid.uuid4().hex[:12], model=model)
        with sessions_lock:
            sessions[session.id] = session
        save()
        return session.summary()

    @app.get("/api/sessions/{session_id}")
    def read_session(session_id: str):
        session = get_session(session_id)
        return {**session.summary(), "messages": session.messages}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        with sessions_lock:
            if sessions.pop(session_id, None) is None:
                raise HTTPException(404, "No such chat")
        save()
        return {"deleted": session_id}

    @app.patch("/api/sessions/{session_id}")
    def change_model(session_id: str, body: ModelChange):
        session = get_session(session_id)
        provider_of(body.model)
        if body.model != session.model:
            with session.lock:
                session.model = body.model
                session.assistant = None  # the new model starts its own conversation
                if session.messages:
                    session.messages.append({"role": "notice", "text": f"Switched to {body.model}. "
                                             "It does not see the earlier messages in this chat."})
            save()
        return session.summary()

    @app.post("/api/sessions/{session_id}/messages")
    def ask(session_id: str, body: Question):
        session = get_session(session_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Empty question")
        if not session.lock.acquire(blocking=False):
            raise HTTPException(409, "This chat is still answering the previous question")

        events: queue.Queue = queue.Queue()
        if session.title == "New chat":
            session.title = _title(text)
        session.messages.append({"role": "user", "text": text})
        save()

        def work():
            try:
                assistant = assistant_for(session)
                assistant.on_tool_call = lambda call: events.put(("tool", _call_dict(call)))
                if hasattr(assistant.backend, "on_wait"):
                    assistant.backend.on_wait = lambda message: events.put(("wait", {"message": message}))
                if hasattr(assistant.backend, "on_draft"):
                    last_sent = [0.0]

                    def on_draft(text: str) -> None:
                        # Clearing always goes through; growing text at most ~12 times a second.
                        if not text:
                            events.put(("draft", {"html": ""}))
                            return
                        now = time.monotonic()
                        if now - last_sent[0] < DRAFT_INTERVAL:
                            return
                        last_sent[0] = now
                        events.put(("draft", {"html": render_markdown(text)}))

                    assistant.backend.on_draft = on_draft
                answer = assistant.ask(text)
                message = {
                    "role": "assistant",
                    "text": answer.text,
                    "html": render_markdown(answer.text),
                    "tool_calls": [_call_dict(c) for c in answer.tool_calls],
                    "model": session.model,
                }
                session.messages.append(message)
                events.put(("answer", message))
            except (AssistantError, HTTPException) as e:
                message = {"role": "error", "text": getattr(e, "detail", None) or str(e)}
                session.messages.append(message)
                events.put(("error", message))
            except Exception as e:  # keep the server up; show the problem in the chat
                message = {"role": "error", "text": f"Something went wrong: {type(e).__name__}: {e}"}
                session.messages.append(message)
                events.put(("error", message))
            finally:
                save()
                session.lock.release()
                events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def stream():
            yield f"event: session\ndata: {json.dumps(session.summary())}\n\n"
            while True:
                try:
                    item = events.get(timeout=15)
                except queue.Empty:
                    yield ": still working\n\n"  # keeps the connection open during long rate-limit waits
                    continue
                if item is None:
                    return
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app
