"""Hosted Gemma 4 via Google's official `google-genai` SDK (Gemini API / AI Studio).

* The API key is read from the environment at construction and handed to the SDK client only.
  It is never stored on this object, logged, or placed in error messages (messages are scrubbed).
* Only `gemma-*` model ids are accepted: a typo can never silently run a Gemini model instead.
* No fallback of any kind. Retries (rate limit, 5xx, timeout, network) are bounded and re-use the
  same model; when they are exhausted the error is raised.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Literal

from .base import RETRYABLE, LLMError, LLMResponse, ToolCall, Turn, Usage, scrub
from .pacing import Pacer, classify_429, retry_after_seconds
from ..config import Settings
from ..credentials import CredentialStoreError

log = logging.getLogger("copilot.llm.gemma")

KEY_HELP = ("No Gemini API key is available. Run `python scripts/setup_gemini_key.py` in your own terminal (hidden prompt; the key is stored in the "
            "Windows Credential Manager), or set GEMINI_API_KEY in the environment of the process that starts the app. "
            "Never paste the key into chat or commit it.")

# JSON-schema keys the Gemini API's Schema type understands (everything else is dropped before sending).
_SCHEMA_KEYS = {"type", "description", "enum", "format", "items", "properties", "required", "nullable", "anyOf", "minimum", "maximum", "minItems", "maxItems",
                "minLength", "maxLength", "pattern"}


def to_gemini_schema(node: Any) -> dict:
    """Convert a pydantic JSON schema (refs already inlined) into the subset the Gemini API accepts."""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if any_of:
            non_null = [n for n in any_of if n.get("type") != "null"]
            nullable = len(non_null) != len(any_of)
            if len(non_null) == 1:
                out = to_gemini_schema(non_null[0])
                if nullable:
                    out["nullable"] = True
                if "description" in node and "description" not in out:
                    out["description"] = node["description"]
                return out
            out = {"anyOf": [to_gemini_schema(n) for n in non_null]}
            if nullable:
                out["nullable"] = True
            if "description" in node:
                out["description"] = node["description"]
            return out
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k not in _SCHEMA_KEYS:
                continue
            if k == "properties":
                out[k] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = to_gemini_schema(v)
            elif k == "type" and isinstance(v, str):
                out[k] = v.upper()
            else:
                out[k] = v
        if out.get("type") == "OBJECT" and "properties" not in out:
            out["properties"] = {}
        return out
    return node


class GemmaApiProvider:
    name = "gemma_api"
    BASE_BACKOFF_S = 1.5
    MAX_BACKOFF_S = 30.0

    def __init__(self, settings: Settings | None = None, *, model: str | None = None, client: Any = None, timeout_s: float | None = None,
                 max_retries: int | None = None, sleep=time.sleep, pacer: Pacer | None = None, rng: random.Random | None = None,
                 max_retry_wait_s: float | None = None, max_total_wait_s: float | None = None):
        self.settings = settings or Settings()
        self.model = (model or self.settings.model or "").strip()
        if not self.model.lower().startswith("gemma-"):
            raise LLMError("invalid_request", f"model id '{scrub(self.model, limit=60)}' is not a Gemma model (expected e.g. gemma-4-26b-a4b-it); refusing to run a different model",
                           provider=self.name, model=self.model)
        self.timeout_s = timeout_s or self.settings.llm_timeout_s
        self.max_retries = max(0, self.settings.llm_max_retries if max_retries is None else max_retries)
        self.max_retry_wait_s = self.settings.llm_max_retry_wait_s if max_retry_wait_s is None else max_retry_wait_s
        self.max_total_wait_s = self.settings.llm_max_total_wait_s if max_total_wait_s is None else max_total_wait_s
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.pacer = pacer or Pacer(self.settings.llm_min_interval_s, sleep=sleep)
        if client is None:
            try:
                key = self.settings.api_key()
            except CredentialStoreError as e:
                raise LLMError("unconfigured", f"{e.message}. {KEY_HELP}", provider=self.name, model=self.model) from None
            if not key:
                raise LLMError("unconfigured", KEY_HELP, provider=self.name, model=self.model)
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000)))
        self._client = client

    def _key_for_scrubbing(self) -> str:
        try:
            return self.settings.api_key() or ""
        except CredentialStoreError:
            return ""

    def __repr__(self) -> str:
        return f"GemmaApiProvider(model={self.model!r})"

    # ------------------------------------------------------------------ public
    def generate(self, turns: list[Turn], *, system: str | None = None, tools: list[dict] | None = None, temperature: float = 0.2,
                 max_output_tokens: int = 2048, thinking: Literal["minimal", "high"] | None = None,
                 tool_mode: Literal["auto", "any", "none"] = "auto") -> LLMResponse:
        # NOTE: max_output_tokens also covers the model's hidden reasoning ("thinking"). A cap smaller than the reasoning
        # leaves no room for the answer (finish_reason=MAX_TOKENS, text=None); see LLMResponse.require_text().
        from google.genai import types
        contents = self._contents(turns, types)
        cfg: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_output_tokens}
        if system:
            cfg["system_instruction"] = system
        if tools:
            cfg["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=types.Schema.model_validate(to_gemini_schema(t["parameters"])))
                for t in tools])]
            if tool_mode != "auto":
                cfg["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY if tool_mode == "any" else types.FunctionCallingConfigMode.NONE))
        if thinking:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH if thinking == "high" else types.ThinkingLevel.MINIMAL)
        config = types.GenerateContentConfig(**cfg)

        attempts, waited, last = 0, 0.0, None
        while attempts <= self.max_retries:
            attempts += 1
            t0 = time.perf_counter()
            try:
                with self.pacer.slot():                          # one request at a time, minimum gap between requests
                    resp = self._client.models.generate_content(model=self.model, contents=contents, config=config)
            except LLMError:
                raise
            except Exception as e:                              # mapped; the raw exception text is never passed on
                last = self._map_exception(e)
                last.attempts = attempts
                if last.kind not in RETRYABLE or attempts > self.max_retries:
                    raise last from None
                delay = self._retry_delay(e, attempts)
                if last.kind == "rate_limit":
                    self.pacer.widen()
                if delay > self.max_retry_wait_s or waited + delay > self.max_total_wait_s:
                    last.retry_after_s = delay                  # too long to wait inside one request: tell the caller when to come back
                    last.message += f" The wait needed (about {delay:.0f}s) is longer than this request may wait, so it was not retried."
                    raise last from None
                last.retry_after_s = delay
                log.warning("gemma call failed (%s), retry %d/%d in %.1fs", last.kind, attempts, self.max_retries, delay)
                self.pacer.penalize(delay)                       # every waiting caller backs off together
                self._sleep(delay)
                waited += delay
                continue
            self.pacer.relax()
            return self._normalize(resp, time.perf_counter() - t0, attempts, max_output_tokens)
        raise last or LLMError("unknown", "no attempt was made", provider=self.name, model=self.model)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        """Seconds to wait before the next attempt. The API's own hint is a floor; otherwise bounded exponential backoff with jitter."""
        hinted = retry_after_seconds(exc)
        if hinted is not None:
            return round(hinted * (1.0 + 0.1 * self._rng.random()), 3)         # never shorter than asked; up to 10% extra spreads callers out
        base = min(self.MAX_BACKOFF_S, self.BASE_BACKOFF_S * (2 ** (attempt - 1)))
        return round(base * (0.75 + 0.5 * self._rng.random()), 3)              # +-25% jitter

    # ------------------------------------------------------------------ request building
    @staticmethod
    def _contents(turns: list[Turn], types) -> list:
        out = []
        for t in turns:
            if t.role == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=t.text or "")]))
            elif t.role == "model":
                if isinstance(t.provider_state, types.Content):
                    out.append(t.provider_state)               # keeps thought signatures intact
                else:
                    parts = ([types.Part(text=t.text)] if t.text else []) + [
                        types.Part(function_call=types.FunctionCall(name=c.name, args=c.args, id=c.id)) for c in t.tool_calls]
                    out.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
            elif t.role == "tool":
                out.append(types.Content(role="user", parts=[types.Part.from_function_response(name=r.name, response=r.content) for r in t.tool_results]))
            else:
                raise LLMError("invalid_request", f"unknown turn role {t.role!r}")
        return out

    # ------------------------------------------------------------------ response normalisation
    def _normalize(self, resp: Any, latency: float, attempts: int, max_output_tokens: int | None = None) -> LLMResponse:
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            pf = getattr(resp, "prompt_feedback", None)
            reason = getattr(pf, "block_reason", None) if pf else None
            if reason:
                raise LLMError("blocked", f"the prompt was blocked by the provider ({getattr(reason, 'name', reason)})", provider=self.name, model=self.model, attempts=attempts)
            raise LLMError("malformed", "the provider returned no candidates", provider=self.name, model=self.model, attempts=attempts)
        c = cands[0]
        parts = list(getattr(getattr(c, "content", None), "parts", None) or [])
        texts: list[str] = []
        calls: list[ToolCall] = []
        thought_chars = 0
        for p in parts:
            if getattr(p, "thought", None):
                thought_chars += len(getattr(p, "text", "") or "")
                continue
            fc = getattr(p, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(ToolCall(name=fc.name, args=_plain(getattr(fc, "args", None)), id=getattr(fc, "id", None)))
            elif getattr(p, "text", None):
                texts.append(p.text)
        text = "".join(texts).strip() or None
        fr = getattr(c, "finish_reason", None)
        finish = getattr(fr, "name", None) or (str(fr) if fr else "UNKNOWN")
        um = getattr(resp, "usage_metadata", None)
        usage = Usage(getattr(um, "prompt_token_count", None), getattr(um, "candidates_token_count", None),
                      getattr(um, "thoughts_token_count", None), getattr(um, "total_token_count", None)) if um else Usage()
        turn = Turn(role="model", text=text, tool_calls=calls, provider_state=getattr(c, "content", None))
        return LLMResponse(text=text, tool_calls=calls, finish_reason=finish, usage=usage, model=self.model, provider=self.name,
                           latency_s=round(latency, 3), attempts=attempts, thought_chars=thought_chars, turn=turn, max_output_tokens=max_output_tokens)

    # ------------------------------------------------------------------ errors
    def _map_exception(self, e: Exception) -> LLMError:
        kw = dict(provider=self.name, model=self.model)
        try:
            from google.genai import errors
            api_err = isinstance(e, errors.APIError)
        except Exception:                                      # pragma: no cover
            api_err = False
        if api_err:
            code = getattr(e, "code", None)
            status = getattr(e, "status", None) or ""
            msg = scrub(getattr(e, "message", "") or "", [self._key_for_scrubbing()], 300)
            low = msg.lower()
            detail = f"HTTP {code} {status}: {msg}".strip()
            if code in (401, 403) or "api key not valid" in low or "api_key_invalid" in low:
                return LLMError("auth", f"the API key was rejected or lacks access ({detail})", status_code=code, **kw)
            if code == 404:
                return LLMError("model_not_found", f"model '{self.model}' was not found for this key ({detail})", status_code=code, **kw)
            if code == 429:
                if classify_429(e) == "daily":
                    return LLMError("quota", f"the daily quota or billing limit is exhausted; waiting will not help ({detail})", status_code=code, **kw)
                return LLMError("rate_limit", f"the request rate or token rate limit was hit ({detail})", status_code=code, **kw)
            if code == 400:
                return LLMError("invalid_request", f"the provider rejected the request ({detail})", status_code=code, **kw)
            if code and code >= 500:
                return LLMError("server", f"the provider had a server error ({detail})", status_code=code, **kw)
            return LLMError("unknown", f"unexpected provider error ({detail})", status_code=code, **kw)
        name = type(e).__name__
        low = name.lower()
        if isinstance(e, TimeoutError) or "timeout" in low:
            return LLMError("timeout", f"the model call exceeded {self.timeout_s:g}s", **kw)
        if any(w in low for w in ("connect", "network", "ssl", "remoteprotocol", "readerror", "proxy", "dns")):
            return LLMError("network", f"could not reach the provider ({name})", **kw)
        return LLMError("unknown", f"unexpected client error ({name})", **kw)


def _plain(x: Any) -> Any:
    """SDK values -> plain JSON-compatible Python."""
    import json
    if x is None:
        return {}
    try:
        return json.loads(json.dumps(dict(x), default=str))
    except (TypeError, ValueError):
        return {}
