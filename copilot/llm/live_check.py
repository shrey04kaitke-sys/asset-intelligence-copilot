"""Live verification of the hosted Gemma 4 provider against the real API.

Run:  python -m copilot.llm.live_check            (exit 0 only if every required step passed)

* Uses the real google-genai SDK and the real key from the environment/.env. No mocks, no fallback
  model, no canned answers: if a step fails the run stops and reports the failure.
* Never prints the key. Only model-generated text (truncated), token counts, tool names/arguments and
  scrubbed error summaries are reported.
* Tool calls proposed by the model are executed only through the validated allow-list registry.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import ROOT, Settings
from .base import LLMError, ToolResultMsg, Turn, scrub


@dataclass
class Step:
    name: str
    required: bool
    ok: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0


@dataclass
class Report:
    model: str
    provider: str = "gemma_api (google-genai)"
    sdk_version: str | None = None
    started_at: str = ""
    steps: list[Step] = field(default_factory=list)
    stopped_early: bool = False
    verdict: str = "NOT RUN"

    def passed(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps if s.required) and not self.stopped_early


def _trim(t: str | None, n: int = 280) -> str | None:
    return None if t is None else (t if len(t) <= n else t[:n] + "…")


def run_live_check(settings: Settings | None = None, *, include_error_paths: bool = True, out_path: Path | None = None) -> Report:
    settings = settings or Settings()
    rep = Report(model=settings.model, started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        import importlib.metadata as md
        rep.sdk_version = md.version("google-genai")
    except Exception:
        pass
    from .gemma_api import GemmaApiProvider

    try:
        prov = GemmaApiProvider(settings)
    except LLMError as e:
        st = Step("configure provider", True, False, error=str(e))
        rep.steps.append(st)
        rep.stopped_early = True
        rep.verdict = "NOT RUN — " + ("no API key available to this process" if e.kind == "unconfigured" else "provider could not be created")
        _save(rep, out_path)
        return rep

    # The registry and a real domain for executing the model's proposed tool calls.
    from ..db import open_source, open_state
    from ..domain import Domain
    from ..feedback import Feedback
    from ..state import StateStore
    from ..tools import ToolContext, build_registry, model_view
    src = open_source(settings)
    if src.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
        from ..ingest import ingest
        ingest(settings, src)
    dom = Domain(src, StateStore(open_state(settings)))
    reg = build_registry()
    ctx = ToolContext(dom, Feedback(dom), user_id=None, allow_writes=False)    # live check is read-only
    specs = reg.function_specs(include_mutating=False)

    def step(name: str, required: bool, fn) -> bool:
        s = Step(name, required)
        rep.steps.append(s)
        t0 = time.perf_counter()
        try:
            s.detail = fn()
            s.ok = True
        except LLMError as e:
            s.error = str(e)
            s.detail = {"error_kind": e.kind, "http_status": e.status_code, "attempts": e.attempts}
        except AssertionError as e:
            s.error = "check failed: " + scrub(str(e), limit=300)
        except Exception as e:                       # never let a bug leak details
            s.error = f"{type(e).__name__}: {scrub(str(e), limit=200)}"
        s.seconds = round(time.perf_counter() - t0, 2)
        if not s.ok and required:
            rep.stopped_early = True
        return s.ok

    # 1 ---------------------------------------------------------------- model exists for this key
    def model_info():
        info = prov._client.models.get(model=prov.model)
        return {"model_resource": getattr(info, "name", None), "input_token_limit": getattr(info, "input_token_limit", None),
                "output_token_limit": getattr(info, "output_token_limit", None)}
    # Informational: an API may not expose metadata for every hosted model; generation below is the real test.
    step("model metadata lookup", False, model_info)

    # 2 ---------------------------------------------------------------- plain generation
    def plain():
        # Budget note: max_output_tokens also covers the model's hidden reasoning, which was ~60-80 tokens even for this
        # trivial prompt. A 64-token cap produced finish_reason=MAX_TOKENS with no answer (see the truncation step below).
        r = prov.generate([Turn("user", "Reply with exactly the single word: pong")], temperature=0.0, max_output_tokens=1024)
        r.require_text()
        assert "pong" in r.text.lower(), f"unexpected reply: {_trim(r.text, 80)!r}"
        assert (r.usage.total_tokens or 0) > 0, "no token usage reported"
        return {"text": _trim(r.text), "finish": r.finish_reason, "model": r.model, "latency_s": r.latency_s, "usage": asdict(r.usage), "attempts": r.attempts}
    if not step("plain generation (real inference)", True, plain):
        return _finish(rep, out_path)

    # 2b --------------------------------------------------------------- a too-small cap must be reported, not silently empty
    def tiny_cap():
        r = prov.generate([Turn("user", "Reply with exactly the single word: pong")], temperature=0.0, max_output_tokens=16)
        out = {"finish": r.finish_reason, "has_text": bool(r.text), "thought_tokens": r.usage.thought_tokens, "thought_chars": r.thought_chars,
               "continuation_available": False, "usage": asdict(r.usage)}
        if r.text:
            out["note"] = "model answered within 16 tokens this time; nothing to report"
            return out
        assert r.truncated, f"empty response but finish_reason={r.finish_reason}, not MAX_TOKENS"
        try:
            r.require_text()
        except LLMError as e:
            assert e.kind == "truncated"
            out["reported_as"] = e.kind
            out["message"] = _trim(e.message, 240)
            return out
        raise AssertionError("require_text() did not raise for an empty truncated response")
    step("small output cap is reported as truncation (observation)", False, tiny_cap)

    # 3 ---------------------------------------------------------------- system instruction
    def system():
        r = prov.generate([Turn("user", "What colour is a clear daytime sky?")], system="Answer with exactly one word, written in capital letters, no punctuation.",
                          temperature=0.0, max_output_tokens=1024)
        r.require_text()
        word = r.text.strip()
        return {"text": _trim(r.text, 60), "followed_instruction": word.isupper() and len(word.split()) == 1, "usage": asdict(r.usage)}
    step("system instruction", False, system)

    # 4 ---------------------------------------------------------------- thinking toggle
    def thinking():
        out = {}
        for lvl in ("minimal", "high"):
            r = prov.generate([Turn("user", "What is 17 multiplied by 23? Reply with just the number.")], thinking=lvl, temperature=0.0, max_output_tokens=1024)
            out[lvl] = {"text": _trim(r.text, 60), "correct": bool(r.text and "391" in r.text), "thought_tokens": r.usage.thought_tokens, "thought_chars": r.thought_chars, "latency_s": r.latency_s}
        assert out["minimal"]["text"] or out["high"]["text"], "no text from either setting"
        return out
    step("thinking level toggle", False, thinking)

    # 5 ---------------------------------------------------------------- function call with every real tool declared
    def call_one():
        sys_p = ("You answer questions about oil wells using the provided tools only. Call a tool to get facts; do not guess. "
                 "Use ISO dates and the exact tool argument names.")
        r = prov.generate([Turn("user", "Is WELL-0001 producing oil right now?")], system=sys_p, tools=specs, temperature=0.0, max_output_tokens=1024)
        assert r.tool_calls, f"model returned no function call (finish={r.finish_reason}, truncated={r.truncated}, thought_tokens={r.usage.thought_tokens}, text={_trim(r.text, 80)!r})"
        tc = r.tool_calls[0]
        env = reg.call(tc.name, tc.args, ctx)                     # validated allow-list registry, not the model, executes
        assert tc.name == "get_asset_status", f"expected get_asset_status, got {tc.name}"
        assert env.ok, f"registry rejected the model's arguments: {env.error.model_dump() if env.error else None}"
        assert env.summary.get("asset_id") == "WELL-0001"
        rep_detail = {"declared_tools": len(specs), "calls": [{"name": c.name, "args": c.args} for c in r.tool_calls], "registry_ok": env.ok,
                      "producing": env.summary.get("producing"), "usage": asdict(r.usage), "latency_s": r.latency_s, "finish": r.finish_reason}
        call_one.state = (r, tc, env, sys_p)
        return rep_detail
    if not step("function call: schemas accepted + arguments valid + registry executes", True, call_one):
        return _finish(rep, out_path)

    # 6 ---------------------------------------------------------------- return the tool result and get a grounded answer
    def roundtrip():
        r1, tc, env, sys_p = call_one.state
        turns = [Turn("user", "Is WELL-0001 producing oil right now?"), r1.turn,
                 Turn("tool", tool_results=[ToolResultMsg(tc.name, model_view(env), tc.id)])]
        r2 = prov.generate(turns, system=sys_p + " Answer in two sentences using only the tool result.", tools=specs, temperature=0.0, max_output_tokens=1024)
        assert r2.text, f"no final text after tool result (finish={r2.finish_reason}, tool_calls={[c.name for c in r2.tool_calls]})"
        low = r2.text.lower()
        grounded = ("not producing" in low or "no" in low.split()[:3] or "isn't" in low or "is not" in low) and ("3.2" in r2.text or "ryan" in low or "ctx-0001-003" in low or "shut" in low)
        assert grounded, f"answer does not reflect the tool result: {_trim(r2.text, 200)!r}"
        return {"final_text": _trim(r2.text, 500), "usage": asdict(r2.usage), "latency_s": r2.latency_s, "finish": r2.finish_reason, "further_tool_calls": [c.name for c in r2.tool_calls]}
    if not step("tool-result round trip (grounded final answer)", True, roundtrip):
        return _finish(rep, out_path)

    # 7 ---------------------------------------------------------------- nested arguments
    def nested():
        r = prov.generate([Turn("user", "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months?")],
                          system="Use the provided tools to answer; do not guess. Use exact tool argument names.", tools=specs, temperature=0.0, max_output_tokens=1024)
        assert r.tool_calls, f"no function call (finish={r.finish_reason})"
        tc = r.tool_calls[0]
        env = reg.call(tc.name, tc.args, ctx)
        ids = [x["asset_id"] for x in env.rows] if env.ok else []
        return {"call": {"name": tc.name, "args": tc.args}, "registry_ok": env.ok, "registry_error": env.error.model_dump() if env.error else None,
                "top_wells_returned": ids, "matches_expected_tool": tc.name == "get_downtime", "usage": asdict(r.usage), "latency_s": r.latency_s}
    step("nested-argument tool call (observation)", False, nested)

    # 8 ---------------------------------------------------------------- real error paths
    if include_error_paths:
        def bad_model():
            p2 = GemmaApiProvider(settings, model="gemma-4-this-model-does-not-exist-it", max_retries=0)
            try:
                p2.generate([Turn("user", "hi")], max_output_tokens=8)
            except LLMError as e:
                assert e.kind in ("model_not_found", "invalid_request"), f"unexpected kind {e.kind}"
                return {"mapped_kind": e.kind, "http_status": e.status_code, "message": _trim(e.message, 200)}
            raise AssertionError("a non-existent model unexpectedly succeeded")
        step("error mapping: unknown model id (real 4xx)", False, bad_model)

    return _finish(rep, out_path)


def _finish(rep: Report, out_path: Path | None) -> Report:
    req_ok = all(s.ok for s in rep.steps if s.required)
    rep.verdict = "PASSED" if (req_ok and not rep.stopped_early) else "FAILED"
    _save(rep, out_path)
    return rep


def _save(rep: Report, out_path: Path | None) -> None:
    path = out_path or (ROOT / "data" / "live_gemma_report.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(rep), indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    rep = run_live_check()
    print(f"model: {rep.model}   provider: {rep.provider}   sdk: {rep.sdk_version}")
    for s in rep.steps:
        mark = "PASS" if s.ok else ("FAIL" if s.required else "note")
        print(f"[{mark}] {s.name} ({s.seconds}s){' — ' + s.error if s.error else ''}")
        if s.detail and (s.ok or s.required):
            print("       " + json.dumps(s.detail, default=str)[:900])
    print(f"VERDICT: {rep.verdict}")
    return 0 if rep.passed() else (2 if rep.verdict.startswith("NOT RUN") else 1)


if __name__ == "__main__":
    sys.exit(main())
