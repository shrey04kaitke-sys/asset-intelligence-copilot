"""Provider-neutral types for model calls.

The orchestrator talks only to these types, so the hosted Gemma API and a local Ollama model are
interchangeable. A provider never falls back to a different model or provider by itself: any failure
raises `LLMError` and the caller decides what to do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ErrorKind = Literal["unconfigured", "auth", "model_not_found", "invalid_request", "rate_limit", "quota", "server", "timeout", "network", "blocked", "malformed", "truncated", "unknown"]
RETRYABLE: set[str] = {"rate_limit", "server", "timeout", "network"}

_KEY_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"(?i)(key|token|authorization)=([^&\s\"']+)"), re.compile(r"sk-[A-Za-z0-9_\-]{16,}")]


def scrub(text: str, secrets: list[str] | None = None, limit: int = 400) -> str:
    """Remove anything that looks like a credential from text that may be logged or shown."""
    out = str(text)
    for s in secrets or []:
        if s and len(s) >= 8:
            out = out.replace(s, "[redacted]")
    for p in _KEY_PATTERNS:
        out = p.sub(lambda m: "[redacted]" if m.re.pattern.startswith("AIza") or m.re.pattern.startswith("sk-") else f"{m.group(1)}=[redacted]", out)
    return out[:limit]


class LLMError(Exception):
    """A failed model call. `message` is safe to show: it never contains credentials."""

    def __init__(self, kind: ErrorKind, message: str, *, status_code: int | None = None, provider: str | None = None, model: str | None = None, attempts: int = 1):
        super().__init__(message)
        self.kind, self.message, self.status_code, self.provider, self.model, self.attempts = kind, message, status_code, provider, model, attempts

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass
class ToolResultMsg:
    name: str
    content: dict[str, Any]
    call_id: str | None = None


@dataclass
class Turn:
    """One conversation turn. `provider_state` keeps the provider's own message object for model turns,
    so provider-specific details (e.g. thought signatures) survive when history is replayed."""
    role: Literal["user", "model", "tool"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResultMsg] = field(default_factory=list)
    provider_state: Any = None


@dataclass
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: Usage
    model: str
    provider: str
    latency_s: float
    attempts: int = 1
    thought_chars: int = 0               # length of hidden reasoning, never exposed to users
    turn: Turn | None = None             # append this to the history to continue the conversation
    max_output_tokens: int | None = None  # the cap that was requested; hidden reasoning counts against it

    @property
    def empty(self) -> bool:
        return not self.text and not self.tool_calls

    @property
    def truncated(self) -> bool:
        """The output cap was reached. For thinking models this can happen before any answer text is written."""
        return self.finish_reason == "MAX_TOKENS"

    def require_text(self) -> str:
        """Answer text, or a precise LLMError saying why there is none (never returns empty)."""
        if self.text:
            return self.text
        if self.truncated:
            used = self.usage.thought_tokens
            raise LLMError("truncated",
                           f"the output limit (max_output_tokens={self.max_output_tokens}) was reached before any answer text"
                           + (f"; hidden reasoning used {used} tokens of it" if used else "")
                           + ". Raise max_output_tokens or lower the thinking level (thinking='minimal'). A continuation is not available: no continuation token was returned.",
                           provider=self.provider, model=self.model)
        raise LLMError("malformed", f"the model returned no answer text (finish_reason={self.finish_reason}"
                       + (", it returned a tool call instead" if self.tool_calls else "") + ")", provider=self.provider, model=self.model)

    @property
    def malformed_tool_call(self) -> bool:
        return self.finish_reason in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL")


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, turns: list[Turn], *, system: str | None = None, tools: list[dict] | None = None, temperature: float = 0.2,
                 max_output_tokens: int = 2048, thinking: Literal["minimal", "high"] | None = None,
                 tool_mode: Literal["auto", "any", "none"] = "auto") -> LLMResponse: ...
