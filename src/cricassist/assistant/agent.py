"""Conversation loop that calls the LLM and runs its tool calls.

`Assistant.ask()` sends a question, runs every tool the model asks for against
the stats database, sends the results back, and repeats until the model
answers in text. Two backends speak the providers' formats: Gemini (the
default, free tier) and Claude. Both keep the whole conversation, so follow-up
questions work.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from cricassist.assistant.prompts import SYSTEM_PROMPT
from cricassist.assistant.tools import TOOLS, Tool, run_tool
from cricassist.config import Settings

MAX_TOOL_ROUNDS = 10  # model turns per question before giving up

# Claude models that accept server-side refusal fallbacks (array form).
_CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5"}


class AssistantError(Exception):
    """A problem the user can fix (missing key, quota, unknown provider)."""


@dataclass
class ToolCall:
    name: str
    args: dict
    is_error: bool
    result: str = ""  # the JSON the model was given


@dataclass
class Answer:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


# Runs one tool call: (name, args) -> (JSON result, is_error).
Executor = Callable[[str, dict], tuple[str, bool]]


class GeminiBackend:
    def __init__(self, api_key: str, model: str, system: str, tools: list[Tool]):
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                        for t in tools
                    ]
                )
            ],
            # We run the tools ourselves so every call is logged and checked.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )
        self.contents = []
        self.on_wait: Callable[[str], None] | None = None  # told when we pause for a rate limit

    @staticmethod
    def _quota_info(error) -> tuple[float | None, bool]:
        """(seconds Gemini asks us to wait, whether a daily quota ran out) from a 429."""
        details = error.details.get("error", {}).get("details", []) if isinstance(error.details, dict) else []
        delay, daily = None, False
        for d in details:
            kind = d.get("@type", "")
            if kind.endswith("RetryInfo"):
                try:
                    delay = float(str(d.get("retryDelay", "")).rstrip("s"))
                except ValueError:
                    pass
            if kind.endswith("QuotaFailure"):
                daily = any("PerDay" in v.get("quotaId", "") for v in d.get("violations", []))
        return delay, daily

    def _generate(self):
        from google.genai import errors

        for attempt in range(4):
            try:
                return self.client.models.generate_content(model=self.model, contents=self.contents, config=self.config)
            except errors.APIError as e:
                if e.code == 429:
                    delay, daily = self._quota_info(e)
                    if daily:
                        raise AssistantError("Gemini's free daily quota for this model is used up. Try again tomorrow, or set CRICASSIST_LLM_MODEL to another model.") from e
                    if attempt < 3:
                        wait = min(delay or 20.0, 60.0) + 1
                        if self.on_wait:
                            self.on_wait(f"Gemini free-tier rate limit; waiting {wait:.0f}s...")
                        time.sleep(wait)
                        continue
                    raise AssistantError("Gemini rate limit reached. Wait a minute and try again.") from e
                if e.code in (500, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                if e.code in (400, 401, 403) and "API key" in str(e):
                    raise AssistantError("Gemini rejected the API key. Check GEMINI_API_KEY in .env.") from e
                raise AssistantError(f"Gemini error {e.code}: {e.message}") from e

    def respond(self, question: str, execute: Executor) -> str:
        types = self._types
        self.contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._generate()
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                reason = candidate.finish_reason if candidate else "no candidates"
                return f"(The model returned no answer: {reason}.)"
            # Keep the model's turn as-is: it carries thought signatures Gemini needs back.
            self.contents.append(candidate.content)
            calls = response.function_calls or []
            if not calls:
                return response.text or ""
            parts = []
            for call in calls:
                result, is_error = execute(call.name, dict(call.args or {}))
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=call.id, name=call.name, response={"error" if is_error else "result": json.loads(result)}
                        )
                    )
                )
            self.contents.append(types.Content(role="user", parts=parts))
        return "(Stopped: the model kept calling tools without answering.)"


class ClaudeBackend:
    def __init__(self, api_key: str, model: str, system: str, tools: list[Tool]):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.system = system
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        self.messages = []

    def _create(self):
        anthropic = self._anthropic
        kwargs = dict(model=self.model, max_tokens=16000, system=self.system, tools=self.tools, messages=self.messages)
        try:
            if self.model in _CLAUDE_FALLBACK_MODELS:
                # On a safety decline, the API retries the same request on the fallback model.
                return self.client.beta.messages.create(
                    **kwargs,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-06-01"],
                    fallbacks=[{"model": "claude-opus-4-8"}],
                )
            return self.client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise AssistantError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.") from e
        except anthropic.RateLimitError as e:
            raise AssistantError("Anthropic rate limit reached. Wait a minute and try again.") from e
        except anthropic.APIStatusError as e:
            raise AssistantError(f"Anthropic error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AssistantError("Could not reach the Anthropic API. Check the internet connection.") from e

    def respond(self, question: str, execute: Executor) -> str:
        self.messages.append({"role": "user", "content": question})
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._create()
            # Append the full content (thinking, tool_use and fallback blocks must go back unchanged).
            self.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                return "(The model declined to answer this request.)"
            if response.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    result, is_error = execute(block.name, dict(block.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result, "is_error": is_error})
                # All results in one message, so the model keeps making parallel calls.
                self.messages.append({"role": "user", "content": results})
                continue
            text = "".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "max_tokens":
                text += "\n\n(Answer cut off at the length limit.)"
            return text
        return "(Stopped: the model kept calling tools without answering.)"


class Assistant:
    """A conversation with the AI model, backed by the stats database."""

    def __init__(self, con, backend, on_tool_call: Callable[[ToolCall], None] | None = None):
        self.con = con
        self.backend = backend
        self.on_tool_call = on_tool_call

    def ask(self, question: str) -> Answer:
        calls: list[ToolCall] = []

        def execute(name: str, args: dict) -> tuple[str, bool]:
            result, is_error = run_tool(self.con, name, args)
            call = ToolCall(name, args, is_error, result)
            calls.append(call)
            if self.on_tool_call:
                self.on_tool_call(call)
            return result, is_error

        text = self.backend.respond(question, execute)
        return Answer(text=text, tool_calls=calls)


def make_backend(settings: Settings, provider: str | None = None, model: str | None = None):
    """The backend for the configured (or given) provider, checking its key is set."""
    from cricassist.config import DEFAULT_MODELS

    provider = (provider or settings.llm_provider).lower()
    if provider not in DEFAULT_MODELS:
        raise AssistantError(f"Unknown provider {provider!r}. Use gemini or claude (CRICASSIST_LLM_PROVIDER).")
    model = model or (settings.llm_model if provider == settings.llm_provider else "") or DEFAULT_MODELS[provider]
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise AssistantError("GEMINI_API_KEY is not set. Add it to .env (see .env.example).")
        return GeminiBackend(settings.gemini_api_key, model, SYSTEM_PROMPT, TOOLS)
    if not settings.anthropic_api_key:
        raise AssistantError("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")
    return ClaudeBackend(settings.anthropic_api_key, model, SYSTEM_PROMPT, TOOLS)
