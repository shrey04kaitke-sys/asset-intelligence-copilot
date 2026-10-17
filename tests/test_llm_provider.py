"""MOCKED unit tests for the Gemma provider layer.

IMPORTANT: these use a fake SDK client. They test error handling, retry bounds, request construction,
response normalisation, secret scrubbing and configuration parsing. They are NOT evidence that the hosted
Gemma model works: that is only established by tests/test_llm_live.py / `python -m copilot.llm.live_check`.
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest
from google.genai import errors, types

from copilot.config import Settings, _decode_env_bytes, _load_dotenv, parse_dotenv
from copilot.llm import LLMError, ToolCall, ToolResultMsg, Turn, get_provider, scrub
from copilot.llm.gemma_api import KEY_HELP, GemmaApiProvider, to_gemini_schema
from copilot.tools import ToolContext, build_registry

from .conftest import MICHAEL

# Built at runtime so no key-shaped literal is ever committed (secret scanners would flag it).
FAKE_KEY = "AI" + "za" + "Sy" + "-fake-test-value-not-a-real-key-" + "0123456789"


# ---------------------------------------------------------------------------------------- fakes
class FakeModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def resp(parts, finish="STOP", usage=(10, 5, 15, None)):
    um = {"prompt_token_count": usage[0], "candidates_token_count": usage[1], "total_token_count": usage[2]}
    if usage[3] is not None:
        um["thoughts_token_count"] = usage[3]
    return types.GenerateContentResponse.model_validate({"candidates": [{"content": {"role": "model", "parts": parts}, "finish_reason": finish}], "usage_metadata": um})


def api_error(code, message, status="STATUS"):
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


def prov(script, **kw):
    sleeps = []
    p = GemmaApiProvider(Settings(model=kw.pop("model", "gemma-4-26b-a4b-it")), client=FakeClient(script), sleep=sleeps.append, **kw)
    p.sleeps = sleeps
    return p


U = [Turn("user", "hello")]


# ---------------------------------------------------------------------------------------- configuration
def test_missing_key_is_unconfigured_and_helpful(monkeypatch):
    for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(LLMError) as ei:
        GemmaApiProvider(Settings())
    assert ei.value.kind == "unconfigured" and ".env" in ei.value.message and "GEMINI_API_KEY" in ei.value.message and ei.value.message == KEY_HELP
    assert not ei.value.retryable


def test_only_gemma_models_are_accepted():
    for bad in ("gemini-2.5-flash", "", "models/gemini-pro", "claude-sonnet"):
        with pytest.raises(LLMError) as ei:
            GemmaApiProvider(Settings(model=bad), client=FakeClient([]))
        assert ei.value.kind == "invalid_request" and "refusing" in ei.value.message
    assert GemmaApiProvider(Settings(model="gemma-4-31b-it"), client=FakeClient([])).model == "gemma-4-31b-it"


def test_default_model_is_the_documented_gemma4_id(monkeypatch):
    monkeypatch.delenv("COPILOT_MODEL", raising=False)
    assert Settings().model == "gemma-4-26b-a4b-it"


def test_factory_never_substitutes_a_provider(monkeypatch):
    for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(LLMError, match="not implemented"):
        get_provider(Settings(provider="ollama"))
    with pytest.raises(LLMError, match="unknown COPILOT_PROVIDER"):
        get_provider(Settings(provider="mock"))
    with pytest.raises(LLMError) as ei:
        get_provider(Settings(provider="gemma_api"))
    assert ei.value.kind == "unconfigured"                      # no key -> error, not a quiet switch to anything else


def test_dotenv_formats_and_key_cleaning(tmp_path, monkeypatch):
    assert parse_dotenv('# c\nA=1\nexport B="two"\nC=\'three\'\nD=four # trailing\nE=\nnoequals\n') == {"A": "1", "B": "two", "C": "three", "D": "four", "E": ""}
    body = "GEMINI_API_KEY=" + FAKE_KEY + "\r\nCOPILOT_MODEL=gemma-4-31b-it\r\n"
    for name, raw in (("utf8", body.encode()), ("bom", b"\xef\xbb\xbf" + body.encode()), ("utf16", body.encode("utf-16")), ("utf16le", body.encode("utf-16-le"))):
        assert parse_dotenv(_decode_env_bytes(raw)).get("GEMINI_API_KEY") == FAKE_KEY, name
    f = tmp_path / ".env"
    f.write_bytes(body.encode("utf-16"))                         # what `echo ... > .env` produces in Windows PowerShell 5.1
    for n in ("GEMINI_API_KEY", "COPILOT_MODEL"):
        monkeypatch.delenv(n, raising=False)
    _load_dotenv(f)
    import os
    assert os.environ["GEMINI_API_KEY"] == FAKE_KEY and os.environ["COPILOT_MODEL"] == "gemma-4-31b-it"
    monkeypatch.setenv("GEMINI_API_KEY", "real-env-wins-" + "x" * 20)
    _load_dotenv(f)
    assert os.environ["GEMINI_API_KEY"].startswith("real-env-wins")
    monkeypatch.setenv("GEMINI_API_KEY", '  "' + FAKE_KEY + '"  ')
    assert Settings().api_key() == FAKE_KEY                      # whitespace and quotes are stripped
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert Settings().api_key() is None


def test_empty_dotenv_value_never_overrides_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    _load_dotenv(f)
    import os
    assert os.environ["GEMINI_API_KEY"] == FAKE_KEY


# ---------------------------------------------------------------------------------------- normalisation
def test_text_response_is_normalised():
    r = prov([resp([{"text": "Hello "}, {"text": "world."}], usage=(12, 4, 16, None))]).generate(U)
    assert r.text == "Hello world." and r.tool_calls == [] and not r.empty and r.finish_reason == "STOP"
    assert (r.usage.prompt_tokens, r.usage.output_tokens, r.usage.total_tokens) == (12, 4, 16)
    assert r.model == "gemma-4-26b-a4b-it" and r.provider == "gemma_api" and r.attempts == 1 and r.latency_s >= 0
    assert r.turn.role == "model" and r.turn.text == "Hello world." and isinstance(r.turn.provider_state, types.Content)


def test_thought_parts_are_counted_but_never_returned():
    r = prov([resp([{"text": "secret chain of thought", "thought": True}, {"text": "391"}], usage=(5, 3, 40, 32))]).generate(U, thinking="high")
    assert r.text == "391" and "secret" not in json.dumps(r.turn.text) and r.thought_chars == len("secret chain of thought") and r.usage.thought_tokens == 32


def test_function_call_parts_become_tool_calls():
    r = prov([resp([{"function_call": {"name": "get_asset_status", "args": {"asset_id": "WELL-0001"}, "id": "c1"}}])]).generate(U)
    assert r.text is None and r.tool_calls == [ToolCall("get_asset_status", {"asset_id": "WELL-0001"}, "c1")] and not r.empty
    r2 = prov([resp([{"text": "checking"}, {"function_call": {"name": "get_downtime", "args": {"scope": {"area": "NE-07"}, "top_n": 3}}}])]).generate(U)
    assert r2.text == "checking" and r2.tool_calls[0].args == {"scope": {"area": "NE-07"}, "top_n": 3}


def test_empty_and_malformed_responses():
    r = prov([resp([])]).generate(U)
    assert r.empty and r.text is None
    r = prov([resp([], finish="MALFORMED_FUNCTION_CALL")]).generate(U)
    assert r.malformed_tool_call and r.empty


def test_no_candidates_blocked_vs_malformed():
    blocked = types.GenerateContentResponse.model_validate({"candidates": [], "prompt_feedback": {"block_reason": "SAFETY"}})
    with pytest.raises(LLMError) as ei:
        prov([blocked]).generate(U)
    assert ei.value.kind == "blocked" and "SAFETY" in ei.value.message
    with pytest.raises(LLMError) as ei:
        prov([types.GenerateContentResponse.model_validate({"candidates": []})]).generate(U)
    assert ei.value.kind == "malformed"


# ---------------------------------------------------------------------------------------- request construction
def test_request_carries_system_tools_thinking_and_tool_mode():
    reg = build_registry()
    specs = reg.function_specs(include_mutating=False)
    p = prov([resp([{"text": "ok"}])])
    p.generate(U, system="be brief", tools=specs, thinking="minimal", tool_mode="any", temperature=0.1, max_output_tokens=300)
    call = p.client_calls = p._client.models.calls[0]
    cfg = call["config"]
    assert call["model"] == "gemma-4-26b-a4b-it"
    assert cfg.system_instruction == "be brief" and cfg.temperature == 0.1 and cfg.max_output_tokens == 300
    assert len(cfg.tools[0].function_declarations) == len(specs) == 19
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.ANY
    p2 = prov([resp([{"text": "ok"}])])
    p2.generate(U)
    c2 = p2._client.models.calls[0]["config"]
    assert c2.tools is None and c2.thinking_config is None and c2.tool_config is None


def test_history_replay_uses_roles_and_function_responses():
    first = prov([resp([{"function_call": {"name": "get_asset_status", "args": {"asset_id": "WELL-0001"}, "id": "c1"}}])]).generate(U)
    p = prov([resp([{"text": "done"}])])
    turns = [Turn("user", "Is WELL-0001 producing?"), first.turn, Turn("tool", tool_results=[ToolResultMsg("get_asset_status", {"summary": {"producing": False}}, "c1")])]
    p.generate(turns)
    contents = p._client.models.calls[0]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1] is first.turn.provider_state                           # original model message reused (thought signatures preserved)
    fr = contents[2].parts[0].function_response
    assert fr.name == "get_asset_status" and fr.response == {"summary": {"producing": False}}
    # a model turn without provider_state is rebuilt from text and calls
    rebuilt = prov([resp([{"text": "x"}])])
    rebuilt.generate([Turn("user", "q"), Turn("model", text="thinking", tool_calls=[ToolCall("get_rules", {"contains": "sr0"})]), Turn("tool", tool_results=[ToolResultMsg("get_rules", {"ok": True})])])
    m = rebuilt._client.models.calls[0]["contents"][1]
    assert m.parts[0].text == "thinking" and m.parts[1].function_call.name == "get_rules" and m.parts[1].function_call.args == {"contains": "sr0"}
    with pytest.raises(LLMError):
        prov([]).generate([Turn("system", "x")])                                # type: ignore[arg-type]


def test_all_registry_tool_schemas_convert_to_sdk_schemas():
    """Local SDK validation of our 22 declarations (no network)."""
    reg = build_registry()
    for s in reg.function_specs():
        sch = types.Schema.model_validate(to_gemini_schema(s["parameters"]))
        types.FunctionDeclaration(name=s["name"], description=s["description"], parameters=sch)
        txt = json.dumps(to_gemini_schema(s["parameters"]))
        for banned in ("additionalProperties", "$ref", "$defs", "\"default\"", "\"title\"", "\"null\""):
            assert banned not in txt, (s["name"], banned)


def test_schema_conversion_details():
    out = to_gemini_schema({"type": "object", "properties": {"a": {"anyOf": [{"type": "string", "maxLength": 5}, {"type": "null"}], "description": "d"},
                                                             "b": {"anyOf": [{"type": "number"}, {"type": "boolean"}]}, "c": {"type": "integer", "default": 3, "minimum": 1}},
                            "required": ["b"], "additionalProperties": False, "title": "T"})
    assert out == {"type": "OBJECT", "properties": {"a": {"type": "STRING", "maxLength": 5, "nullable": True, "description": "d"},
                                                    "b": {"anyOf": [{"type": "NUMBER"}, {"type": "BOOLEAN"}]}, "c": {"type": "INTEGER", "minimum": 1}}, "required": ["b"]}
    assert to_gemini_schema({"type": "object"}) == {"type": "OBJECT", "properties": {}}


# ---------------------------------------------------------------------------------------- errors, retries, no fallback
@pytest.mark.parametrize("err,kind,retried", [
    (api_error(401, "API key not valid. Please pass a valid API key.", "UNAUTHENTICATED"), "auth", False),
    (api_error(403, "Permission denied", "PERMISSION_DENIED"), "auth", False),
    (api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"), "auth", False),
    (api_error(400, "Invalid JSON payload received. Unknown name", "INVALID_ARGUMENT"), "invalid_request", False),
    (api_error(404, "models/gemma-x is not found", "NOT_FOUND"), "model_not_found", False),
    (api_error(429, "Resource has been exhausted (e.g. check quota).", "RESOURCE_EXHAUSTED"), "quota", False),
    (api_error(429, "Too many requests, slow down", "RESOURCE_EXHAUSTED"), "rate_limit", True),
    (api_error(500, "Internal error", "INTERNAL"), "server", True),
    (api_error(503, "The model is overloaded", "UNAVAILABLE"), "server", True),
    (httpx.ReadTimeout("timed out"), "timeout", True),
    (TimeoutError("x"), "timeout", True),
    (httpx.ConnectError("dns failure"), "network", True),
    (RuntimeError("boom"), "unknown", False),
])
def test_error_mapping_and_retry_policy(err, kind, retried):
    errs = [err, err, err]
    p = prov(list(errs), max_retries=2)
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    e = ei.value
    assert e.kind == kind and e.retryable == retried and e.provider == "gemma_api" and e.model == "gemma-4-26b-a4b-it"
    calls = len(p._client.models.calls)
    assert calls == (3 if retried else 1) and e.attempts == calls
    assert len(p.sleeps) == (2 if retried else 0)
    assert all(c["model"] == "gemma-4-26b-a4b-it" for c in p._client.models.calls)       # same model every time: no fallback
    assert "Traceback" not in e.message and "RuntimeError" not in e.message.replace("unexpected client error (RuntimeError)", "")


def test_transient_failure_then_success_is_retried_with_backoff():
    p = prov([api_error(503, "overloaded", "UNAVAILABLE"), api_error(500, "oops", "INTERNAL"), resp([{"text": "ok"}])], max_retries=2)
    r = p.generate(U)
    assert r.text == "ok" and r.attempts == 3 and p.sleeps == [1.5, 3.0]
    assert [c["model"] for c in p._client.models.calls] == ["gemma-4-26b-a4b-it"] * 3


def test_retries_are_bounded_and_zero_retries_means_one_call():
    p = prov([api_error(503, "x", "UNAVAILABLE")] * 5, max_retries=0)
    with pytest.raises(LLMError):
        p.generate(U)
    assert len(p._client.models.calls) == 1 and p.sleeps == []


def test_failure_never_produces_a_response_object():
    """A failed hosted call must raise: no canned text, no other model."""
    p = prov([api_error(500, "down", "INTERNAL")] * 3)
    with pytest.raises(LLMError):
        p.generate(U)


def test_secrets_are_scrubbed_from_error_messages(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    created = {}

    class SpyClient(FakeClient):
        pass
    import google.genai as genai
    monkeypatch.setattr(genai, "Client", lambda api_key, http_options=None: created.setdefault("c", SpyClient([api_error(400, f"bad request key={FAKE_KEY} and {FAKE_KEY} https://x/?key={FAKE_KEY}", "INVALID_ARGUMENT")])))
    p = GemmaApiProvider(Settings(), sleep=lambda s: None)
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    assert FAKE_KEY not in str(ei.value) and FAKE_KEY not in ei.value.message and "AIzaSy" not in ei.value.message
    assert FAKE_KEY not in repr(vars(p)) and FAKE_KEY not in repr(p) and FAKE_KEY not in str(vars(p.settings))   # the provider holds no copy of the key


def test_scrub_helper():
    s = scrub(f"call failed: key={FAKE_KEY}; Authorization=Bearer abc; token=xyz789 and {FAKE_KEY}", [FAKE_KEY])
    assert FAKE_KEY not in s and "xyz789" not in s and "AIza" not in s
    assert len(scrub("x" * 1000, limit=50)) == 50


# ---------------------------------------------------------------------------------------- model output stays behind the registry
def test_model_proposed_calls_are_validated_by_the_registry():
    """Mock-driven: whatever a model proposes is executed only via the allow-list registry."""
    reg = build_registry()
    p = prov([resp([{"function_call": {"name": "os.system", "args": {"cmd": "calc"}}}]),
              resp([{"function_call": {"name": "get_asset_status", "args": {"asset_id": "WELL-0001", "sql": "DROP TABLE x"}}}])])
    for _ in range(2):
        r = p.generate(U, tools=reg.function_specs())
        tc = r.tool_calls[0]
        env = reg.call(tc.name, tc.args, None)                     # context unused: rejected before any handler runs
        assert not env.ok and env.error.code in ("unknown_tool", "invalid_arguments")


# ---------------------------------------------------------------------------------------- live-check wiring without a key
def test_live_check_reports_not_run_without_key_and_makes_no_network_call(monkeypatch, tmp_path):
    for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    import google.genai as genai

    def boom(*a, **k):
        raise AssertionError("a client must not be created without a key")
    monkeypatch.setattr(genai, "Client", boom)
    from copilot.llm.live_check import run_live_check
    rep = run_live_check(Settings(data_dir=tmp_path), out_path=tmp_path / "r.json")
    assert rep.verdict.startswith("NOT RUN") and not rep.passed() and rep.steps[0].error.startswith("[unconfigured]")
    saved = json.loads((tmp_path / "r.json").read_text())
    assert saved["verdict"].startswith("NOT RUN") and "AIza" not in json.dumps(saved)
