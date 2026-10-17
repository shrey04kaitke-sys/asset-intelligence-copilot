"""Request orchestration: question -> bounded Gemma tool loop -> validated, cited, structured answer.

What the model decides: which registered tools to call, with what arguments, which visual type to show, and the wording
of the answer. What the application enforces (none of it depends on the model behaving):
  * only registry tools can run; arguments are schema-validated; unknown tools / extra fields are rejected;
  * all numbers, rules, deadlines, authorization, corrections and visual specs come from deterministic backend code;
  * writes need the user's own words (verbatim quote) and, for tools listed in `require_confirmation_for`, an explicit
    `confirm()` step;
  * rounds, tool calls and per-round calls are capped; truncated or empty model output is an error, never a "success";
  * citations that do not match retrieved evidence are removed and reported; numbers the tools never returned are flagged;
  * private memory is read and written only for the signed-in user.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import Domain
from .feedback import Feedback
from .llm.base import LLMError, LLMProvider, LLMResponse, ToolResultMsg, Turn
from .prompts import CHOOSE_VISUAL, build_system_prompt
from .state import StateStore
from .tools import EvidenceItem, ToolContext, ToolEnvelope, ToolRegistry, model_view, select_visual
from .visuals import VISUAL_TYPES

log = logging.getLogger("copilot.orchestrator")

VisualName = Literal["card", "table", "line", "bar", "map", "heatmap", "timeline", "donut"]


# ================================================================================ contract
class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    visual: VisualName | None = Field(None, description="A visual the user explicitly asked for (otherwise it is read from the question or chosen)")
    thinking: Literal["minimal", "high"] | None = None


class Citation(BaseModel):
    ref: str
    type: str
    id: str
    title: str
    kind: str
    path: str | None = None
    fields: dict[str, Any] = {}


class TraceStep(BaseModel):
    step: int
    kind: Literal["model", "tool", "control"]
    name: str | None = None
    ok: bool = True
    code: str | None = None
    detail: str | None = None
    rows: int | None = None
    seconds: float = 0.0


class PendingAction(BaseModel):
    proposal_id: str
    tool: str
    summary: str


class UsageTotals(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    seconds: float = 0.0


class ErrorInfo(BaseModel):
    code: str
    message: str
    retry_after_s: float | None = None      # when a rate limit caused the failure: how long to wait before asking again


class AskResponse(BaseModel):
    ok: bool
    conversation_id: str | None = None
    question: str
    answer: str | None = None
    citations: list[Citation] = []
    evidence_consulted: int = 0
    visual: dict[str, Any] | None = None
    visual_note: str | None = None
    visual_source: dict[str, Any] | None = None
    uncertainty: list[str] = []
    conflicts: list[str] = []
    assumptions: list[str] = []
    warnings: list[str] = []
    needs_confirmation: list[str] = []
    pending_actions: list[PendingAction] = []
    data: dict[str, Any] = {}
    trace: list[TraceStep] = []
    usage: UsageTotals = UsageTotals()
    model: str | None = None
    thinking: str | None = None
    error: ErrorInfo | None = None


class ConfirmResponse(BaseModel):
    ok: bool
    status: Literal["executed", "cancelled", "failed"]
    message: str
    warnings: list[str] = []
    citations: list[Citation] = []
    error: ErrorInfo | None = None


@dataclass
class OrchestratorConfig:
    max_rounds: int = 6                 # model calls that may propose tools before the answer is forced
    max_tool_calls: int = 10            # executed tool calls per question
    max_calls_per_round: int = 4
    max_output_tokens: int = 2048       # covers Gemma's hidden reasoning as well as the answer
    max_output_tokens_cap: int = 8192   # ceiling for the single retry after truncation
    temperature: float = 0.1
    thinking: Literal["minimal", "high"] | None = None
    history_messages: int = 6
    require_confirmation_for: frozenset[str] = frozenset()
    escalate_on_truncation: bool = True
    pending_ttl_s: int = 900
    max_answer_chars: int = 8000


# ================================================================================ small deterministic helpers
REF = re.compile(r"\[([a-z_]+:[^\]\s]+)\]")
_VISUAL_PATTERNS = [
    (r"\bheat ?map\b", "heatmap"), (r"\b(status\s+)?card\b", "card"), (r"\btable\b", "table"), (r"\bline\s+(chart|graph|plot)\b", "line"),
    (r"\bbar\s+(chart|graph)\b", "bar"), (r"\bmap\b", "map"), (r"\btimeline\b", "timeline"), (r"\b(pie|donut|doughnut)(\s+chart)?\b", "donut"),
]
_NON_DECLARATIVE_START = re.compile(r"^(show|list|give|tell|what|which|who|whom|whose|how|when|where|why|is|are|was|were|does|do|did|can|could|would|should|will|"
                                    r"please|display|plot|draw|compare|find|get|count|summari[sz]e|explain|has|have|had)\b", re.I)
_MODEL_SLIPS = frozenset({"unknown_tool", "invalid_arguments", "invalid_window", "window_too_wide", "unknown_equipment_kind", "unknown_area", "unknown_well_type"})
CONFLICT_RE = re.compile(r"disagree|competing|conflict|contradict|out of date|stale|differs|unconfirmed|not match|recorded_status says|inspections are the truth", re.I)
_ID = re.compile(r"\b[A-Za-z]{1,5}-[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b|\bT\d{3}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def detect_visual_request(question: str) -> str | None:
    """A visual the user named in their own words ('as a table', 'bar chart'); earliest mention wins."""
    best: tuple[int, str] | None = None
    for rx, name in _VISUAL_PATTERNS:
        m = re.search(rx, question, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


def has_declarative(question: str) -> bool:
    """True if the message contains a statement (not just questions/commands). Recording tools are offered only then."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", question.strip()):
        s = s.strip()
        if len(s.split()) >= 3 and not s.endswith("?") and not _NON_DECLARATIVE_START.match(s):
            return True
    return False


def _norm_num(x: str) -> str:
    try:
        return f"{abs(float(x.replace(',', ''))):g}"
    except ValueError:
        return x


def unverified_numbers(answer: str, payloads: list[str], question: str) -> list[str]:
    """Numbers in the answer that appear nowhere in the tool results or the user's own message."""
    text = REF.sub(" ", answer)
    text = _DATE.sub(" ", text)
    text = _ID.sub(" ", text)
    found = {_norm_num(n) for n in _NUM.findall(text)}
    allowed = {_norm_num(x) for p in payloads + [question] for x in re.findall(r"\d+(?:\.\d+)?", p)}
    return sorted((n for n in found if n not in allowed and n not in {"0", "1"}), key=lambda s: float(s))


@dataclass
class _Call:
    call_id: str
    name: str
    args: dict
    env: ToolEnvelope | None
    pending: PendingAction | None = None


@dataclass
class _Run:
    req: AskRequest
    conversation_id: str | None = None
    trace: list[TraceStep] = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)
    calls: dict[str, _Call] = field(default_factory=dict)
    pool: dict[str, EvidenceItem] = field(default_factory=dict)
    chosen: tuple[str, str] | None = None
    seq: int = 0
    executed: int = 0
    malformed_retries: int = 0
    notes: list[str] = field(default_factory=list)
    pending: list[PendingAction] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def step(self, kind, name=None, ok=True, code=None, detail=None, rows=None, seconds=0.0):
        self.trace.append(TraceStep(step=len(self.trace) + 1, kind=kind, name=name, ok=ok, code=code, detail=detail, rows=rows, seconds=round(seconds, 3)))


_LLM_CODES = {"unconfigured": "llm_not_configured", "auth": "llm_auth", "model_not_found": "llm_model_not_found", "rate_limit": "rate_limited", "quota": "rate_limited",
              "server": "llm_unavailable", "timeout": "llm_unavailable", "network": "llm_unavailable", "truncated": "truncated", "blocked": "blocked",
              "malformed": "llm_empty_response", "invalid_request": "llm_error", "unknown": "llm_error"}


class PendingStore:
    """Write proposals awaiting an explicit user confirmation (in memory, per process, expiring)."""

    def __init__(self, ttl_s: int = 900):
        self.ttl_s, self._d, self._lock = ttl_s, {}, threading.Lock()

    def add(self, user_id: str, tool: str, args: dict, user_message: str, conversation_id: str | None, summary: str) -> PendingAction:
        pid = "PEND-" + uuid.uuid4().hex[:10]
        with self._lock:
            self._purge()
            self._d[pid] = dict(user_id=user_id, tool=tool, args=args, user_message=user_message, conversation_id=conversation_id, summary=summary, at=time.time())
        return PendingAction(proposal_id=pid, tool=tool, summary=summary)

    def take(self, proposal_id: str, user_id: str) -> dict | None:
        with self._lock:
            self._purge()
            p = self._d.get(proposal_id)
            if not p or p["user_id"] != user_id:                 # another user's proposal is indistinguishable from a missing one
                return None
            return self._d.pop(proposal_id)

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v["at"] > self.ttl_s]:
            del self._d[k]


# ================================================================================ orchestrator
class Orchestrator:
    def __init__(self, domain: Domain, state: StateStore, feedback: Feedback, registry: ToolRegistry, provider: LLMProvider,
                 config: OrchestratorConfig | None = None):
        self.domain, self.state, self.feedback, self.registry, self.provider = domain, state, feedback, registry, provider
        self.cfg = config or OrchestratorConfig()
        self.pending = PendingStore(self.cfg.pending_ttl_s)

    # ------------------------------------------------------------------ public
    def ask(self, req: AskRequest) -> AskResponse:
        run = _Run(req)
        user = self.domain.users().get(req.user_id)
        if not user:
            return self._fail(run, "unknown_user", "the user id is not in the user directory")
        try:
            run.conversation_id = self.state.ensure_conversation(req.user_id, req.conversation_id, req.question[:60])
        except PermissionError:
            return self._fail(run, "forbidden", "that conversation belongs to another user")
        requested = req.visual or detect_visual_request(req.question)
        writes_ok = has_declarative(req.question)
        ctx = ToolContext(self.domain, self.feedback, user_id=req.user_id, conversation_id=run.conversation_id, user_message=req.question,
                          require_grounding=True, allow_writes=writes_ok)
        system = build_system_prompt(user=user, as_of=self.domain.as_of, as_of_basis=self.domain.as_of_basis,
                                     areas=[a["name"] for a in self.domain.areas().values()], well_types=self.domain.well_types(),
                                     requested_visual=requested, writes_allowed=writes_ok)
        specs = self.registry.function_specs(include_mutating=writes_ok) + [CHOOSE_VISUAL]
        turns = self._history(run.conversation_id, req.user_id) + [Turn("user", text=req.question)]
        thinking = req.thinking or self.cfg.thinking
        try:
            text = self._loop(run, ctx, turns, system, specs, thinking)
            return self._finalize(run, user, text, requested, thinking)
        except LLMError as e:
            run.step("model", name=self.provider.model, ok=False, code=e.kind)
            return self._fail(run, _LLM_CODES.get(e.kind, "llm_error"), e.message, thinking, retry_after=e.retry_after_s)
        except Exception as e:                                       # never leak internals
            log.exception("orchestration failed (%s)", type(e).__name__)
            return self._fail(run, "internal_error", "the request could not be completed; the failure was logged", thinking)

    def confirm(self, user_id: str, proposal_id: str, approve: bool = True) -> ConfirmResponse:
        if user_id not in self.domain.users():
            return ConfirmResponse(ok=False, status="failed", message="unknown user", error=ErrorInfo(code="unknown_user", message="the user id is not in the user directory"))
        p = self.pending.take(proposal_id, user_id)
        if not p:
            return ConfirmResponse(ok=False, status="failed", message="no such pending action (it may have expired or belong to someone else)",
                                   error=ErrorInfo(code="not_found", message="no such pending action"))
        if not approve:
            self.state.audit(user_id, "write_cancelled", "proposal", proposal_id, {"tool": p["tool"]})
            return ConfirmResponse(ok=True, status="cancelled", message="Cancelled. Nothing was recorded.")
        ctx = ToolContext(self.domain, self.feedback, user_id=user_id, conversation_id=p["conversation_id"], user_message=p["user_message"], require_grounding=True, allow_writes=True)
        env = self.registry.call(p["tool"], p["args"], ctx)
        self.state.audit(user_id, "write_confirmed", "proposal", proposal_id, {"tool": p["tool"], "ok": env.ok, "code": env.error.code if env.error else None})
        if not env.ok:
            return ConfirmResponse(ok=False, status="failed", message=env.error.message if env.error else "failed", error=ErrorInfo(code=env.error.code, message=env.error.message) if env.error else None)
        return ConfirmResponse(ok=True, status="executed", message=" ".join(env.narrative[:2]), warnings=env.warnings, citations=[self._citation(e) for e in env.evidence[:20]])

    # ------------------------------------------------------------------ the bounded loop
    def _loop(self, run: _Run, ctx: ToolContext, turns: list[Turn], system: str, specs: list[dict], thinking) -> str:
        for _ in range(self.cfg.max_rounds):
            r = self._model_call(run, turns, system, specs, thinking, "auto")
            turns.append(r.turn)
            if r.tool_calls:
                results = [self._run_tool(run, ctx, tc, i) for i, tc in enumerate(r.tool_calls)]
                turns.append(Turn("tool", tool_results=results))
                continue
            if r.text:
                return r.text
            if r.malformed_tool_call and run.malformed_retries < 1:
                run.malformed_retries += 1
                run.step("model", name="malformed_tool_call", ok=False, code="malformed_tool_call", detail="asked the model to repeat the call")
                turns.append(Turn("user", text="Your last function call was malformed. Call the function again with valid JSON arguments, or answer in text."))
                continue
            r.require_text()                                         # raises a precise LLMError (truncated / empty)
        # rounds exhausted: force an answer from what has been gathered, without tools
        run.notes.append(f"The tool-call limit ({self.cfg.max_rounds} rounds) was reached; this answer uses only what had been gathered.")
        turns.append(Turn("user", text="Stop calling tools. Answer the user now using only the tool results above, and say clearly what could not be determined."))
        r = self._model_call(run, turns, system, specs, thinking, "none")
        turns.append(r.turn)
        return r.require_text()

    def _model_call(self, run: _Run, turns, system, specs, thinking, tool_mode) -> LLMResponse:
        budget = self.cfg.max_output_tokens
        for attempt in (1, 2):
            t0 = time.perf_counter()
            r = self.provider.generate(turns, system=system, tools=specs, temperature=self.cfg.temperature, max_output_tokens=budget, thinking=thinking, tool_mode=tool_mode)
            run.usage.model_calls += 1
            run.usage.prompt_tokens += r.usage.prompt_tokens or 0
            run.usage.output_tokens += r.usage.output_tokens or 0
            run.usage.thought_tokens += r.usage.thought_tokens or 0
            run.step("model", name=r.model, ok=not r.truncated, code="truncated" if r.truncated else None,
                     detail=f"finish={r.finish_reason}; tokens prompt={r.usage.prompt_tokens} out={r.usage.output_tokens} thought={r.usage.thought_tokens}; attempts={r.attempts}",
                     seconds=time.perf_counter() - t0)
            if not r.truncated:
                return r
            if attempt == 1 and self.cfg.escalate_on_truncation and budget < self.cfg.max_output_tokens_cap:
                budget = min(self.cfg.max_output_tokens_cap, budget * 2)      # documented remedy: more room for reasoning + answer
                continue
            break
        raise LLMError("truncated", f"the model's reply was cut off at the output limit (max_output_tokens={budget}); the incomplete reply was discarded. "
                       "Try again, or raise the output budget / lower the thinking level.", provider=self.provider.name, model=self.provider.model)

    # ------------------------------------------------------------------ tools
    def _run_tool(self, run: _Run, ctx: ToolContext, tc, index: int) -> ToolResultMsg:
        run.seq += 1
        call_id = f"c{run.seq}"
        t0 = time.perf_counter()
        name, args = tc.name, tc.args if isinstance(tc.args, dict) else {}
        if name == "choose_visual":
            return ToolResultMsg(name, self._choose_visual(run, args, call_id, t0), tc.id)
        env: ToolEnvelope | None = None
        pending = None
        if index >= self.cfg.max_calls_per_round:
            view: dict = {"ok": False, "error": {"code": "too_many_calls", "message": f"at most {self.cfg.max_calls_per_round} tool calls per turn; call the rest in your next turn"}}
        elif run.executed >= self.cfg.max_tool_calls:
            view = {"ok": False, "error": {"code": "tool_budget", "message": f"the limit of {self.cfg.max_tool_calls} tool calls for this question was reached; answer with what you have"}}
        else:
            tool = self.registry.get(name)
            if tool and tool.mutating and name in self.cfg.require_confirmation_for:
                err, norm = self.registry.dry_run(name, args, ctx)
                run.executed += 1
                if err:
                    env = err
                    view = model_view(env)
                else:
                    pending = self.pending.add(ctx.user_id or "", name, norm or {}, ctx.user_message or "", ctx.conversation_id, self._summarise_write(name, norm or {}))
                    run.pending.append(pending)
                    view = {"ok": True, "tool": name, "status": "awaiting_user_confirmation", "proposal_id": pending.proposal_id, "summary": pending.summary,
                            "instruction": "Nothing has been recorded yet. Tell the user what would be recorded and that they must confirm it."}
            else:
                env = self.registry.call(name, args, ctx)
                run.executed += 1
                run.usage.tool_calls += 1
                view = model_view(env)
                for e in env.evidence:
                    run.pool.setdefault(e.ref, e)
        view = {"call_id": call_id, **view}
        run.calls[call_id] = _Call(call_id, name, args, env, pending)
        ok = bool(view.get("ok"))
        run.step("tool", name=name, ok=ok, code=(view.get("error") or {}).get("code"), rows=(env.rows_total if env and env.ok else None),
                 detail=("awaiting confirmation" if pending else None), seconds=time.perf_counter() - t0)
        return ToolResultMsg(name, view, tc.id)

    def _choose_visual(self, run: _Run, args: dict, call_id: str, t0: float) -> dict:
        target, vtype = str(args.get("call_id", "")), str(args.get("visual_type", ""))
        c = run.calls.get(target)
        if not c or not c.env or not c.env.ok:
            out = {"ok": False, "error": {"code": "unknown_call", "message": f"call_id '{target[:20]}' is not a successful tool result from this turn"}}
        elif vtype not in c.env.visuals:
            out = {"ok": False, "error": {"code": "visual_unavailable", "message": f"{c.name} offers: {', '.join(c.env.visuals) or 'no visuals'}"}}
        else:
            run.chosen = (target, vtype)
            out = {"ok": True, "chosen": {"call_id": target, "visual_type": vtype}}
        run.step("control", name="choose_visual", ok=out["ok"], code=(out.get("error") or {}).get("code"), seconds=time.perf_counter() - t0)
        return out

    @staticmethod
    def _summarise_write(name: str, a: dict) -> str:
        if name == "record_correction":
            return f"correct {a.get('field')} to {a.get('new_value')} on {a.get('inspection_id') or a.get('asset_id')} {a.get('date') or ''} (reason: {a.get('reason')})".replace("  ", " ")
        if name == "record_rule_override":
            return f"set {a.get('rule_key')} = {a.get('value')} for {a.get('scope')} {a.get('scope_value')} (reason: {a.get('reason')})"
        if name == "record_claim":
            return f"record a {a.get('visibility', 'shared')} {a.get('claim_type')} claim on {a.get('asset_id')}: {a.get('text')}"
        return name

    # ------------------------------------------------------------------ result assembly
    def _finalize(self, run: _Run, user: dict, text: str, requested: str | None, thinking) -> AskResponse:
        req = run.req
        text = (text or "").strip()[: self.cfg.max_answer_chars]
        warnings: list[str] = list(run.notes)
        cited = list(dict.fromkeys(REF.findall(text)))
        valid = [r for r in cited if r in run.pool]
        invalid = [r for r in cited if r not in run.pool]
        for r in invalid:
            text = re.sub(r"\s*\[" + re.escape(r) + r"\]", "", text)
        if invalid:
            warnings.append(f"{len(invalid)} citation(s) the model gave did not match any retrieved evidence and were removed")
        if run.pool and not valid and any(c.env and c.env.ok for c in run.calls.values()):
            warnings.append("the answer carries no citations; see 'evidence consulted' for the records the tools returned")
        ok_envs = [c for c in run.calls.values() if c.env and c.env.ok]
        payloads = [json.dumps(model_view(c.env), default=str) for c in ok_envs]
        bad_nums = unverified_numbers(text, payloads, req.question)
        if bad_nums:
            warnings.append("numbers in the answer that no tool result contains (verify before relying on them): " + ", ".join(bad_nums[:8]))
        if not ok_envs and not run.pending:
            warnings.append("no tool returned data for this answer; treat it as unsupported")

        visual, vnote, vsource = self._pick_visual(run, requested)
        uncertainty, conflicts, assumptions, confirm = self._collect_flags(run)
        warnings += [w for c in ok_envs for w in c.env.warnings if w not in warnings and not CONFLICT_RE.search(w) and not w.startswith("Please confirm")]

        self._remember(run, req, ok_envs)
        conv = run.conversation_id
        self.state.add_message(conv, req.user_id, "user", req.question)
        self.state.add_message(conv, req.user_id, "assistant", text, {"tools": [c.name for c in run.calls.values()], "citations": valid, "visual": (vsource or {}).get("type")})
        run.usage.seconds = round(time.perf_counter() - run.started, 3)
        self.state.audit(req.user_id, "ask", "conversation", conv or "", {"tools": [c.name for c in run.calls.values()], "model_calls": run.usage.model_calls,
                                                                          "tool_calls": run.usage.tool_calls, "ok": True, "pending": [p.proposal_id for p in run.pending]})
        return AskResponse(
            ok=True, conversation_id=conv, question=req.question, answer=text, citations=[self._citation(run.pool[r]) for r in valid],
            evidence_consulted=len(run.pool), visual=visual, visual_note=vnote, visual_source=vsource, uncertainty=uncertainty, conflicts=conflicts,
            assumptions=assumptions, warnings=warnings, needs_confirmation=confirm, pending_actions=run.pending,
            data={"as_of": self.domain.as_of.isoformat(), "as_of_basis": self.domain.as_of_basis, "last_ingest_at": self.domain.last_ingest_at},
            trace=run.trace, usage=run.usage, model=self.provider.model, thinking=thinking)

    def _pick_visual(self, run: _Run, requested: str | None):
        env, src = None, None
        if run.chosen and run.calls.get(run.chosen[0]) and run.calls[run.chosen[0]].env:
            c = run.calls[run.chosen[0]]
            env, src = c.env, c
            if requested and requested in env.visuals:
                spec, note = select_visual(env, requested)
            else:
                spec, note = env.visuals.get(run.chosen[1]), None
                if requested and requested not in env.visuals:
                    note = f"a {requested} cannot be drawn from this result; showing a {run.chosen[1]} instead (available: {', '.join(env.visuals)})"
        else:
            cands = [c for c in run.calls.values() if c.env and c.env.ok and c.env.visuals and not c.env.mutating and c.name != "recall_my_context"]
            if not cands:
                return None, (f"no data was available to draw a {requested}" if requested else None), None
            src = cands[-1]
            env = src.env
            spec, note = select_visual(env, requested)
        if spec is None:
            return None, note, None
        return spec, note, {"tool": src.name, "call_id": src.call_id, "type": spec.get("type")}

    def _collect_flags(self, run: _Run):
        uncertainty, conflicts, assumptions, confirm = [], [], [], []
        ok_by_tool = {c.name for c in run.calls.values() if c.env and c.env.ok}
        got_data = any(c.env and c.env.ok and not c.env.mutating for c in run.calls.values())
        for c in run.calls.values():
            e = c.env
            if e is None:
                continue
            if not e.ok:
                # A malformed guess at a read tool is the model's own mistake: once real data came back it is noise.
                # Failed writes, exhausted budgets and genuine failures always matter to the user.
                model_slip = e.error.code in _MODEL_SLIPS and not e.mutating and got_data
                if c.name not in ok_by_tool and c.name != "choose_visual" and not model_slip:
                    uncertainty.append(f"{c.name} could not be completed ({e.error.code}): {e.error.message[:160]}")
                continue
            for w in e.warnings:
                if w.startswith("Please confirm"):
                    confirm.append(w[len("Please confirm:"):].strip() if w.startswith("Please confirm:") else w)
                elif CONFLICT_RE.search(w):
                    conflicts.append(w)
                elif "no recorded reason" in w.lower() or "cannot be determined" in w.lower():
                    uncertainty.append(w)
            for a in e.assumptions:
                assumptions.append(a)
            s = e.summary
            if s.get("no_recorded_reason"):
                uncertainty.append(f"{s.get('asset_id', 'a well')}: no recorded reason explains the stop")
            if e.tool == "list_asset_status" and s.get("not_producing_without_reason"):
                uncertainty.append(f"{s['not_producing_without_reason']} not-producing well(s) have no recorded reason")
            if e.tool == "record_claim" and s.get("verification") in ("contradicted", "unverified"):
                (conflicts if s.get("verification") == "contradicted" else uncertainty).append(f"claim {s.get('claim_id')} is {s.get('verification')}: {s.get('detail')}")
            if e.mutating and s.get("needs_confirmation") and isinstance(s["needs_confirmation"], bool):
                confirm.append(f"{s.get('claim_id', 'the new statement')} is recorded as unconfirmed; please confirm or correct it")
        for p in run.pending:
            confirm.append(f"pending {p.proposal_id}: {p.summary}")
        dedupe = lambda xs: list(dict.fromkeys(xs))
        return dedupe(uncertainty), dedupe(conflicts), dedupe(assumptions), dedupe(confirm)

    def _remember(self, run: _Run, req: AskRequest, ok_envs) -> None:
        n = 0
        for c in ok_envs:
            if c.env.mutating or c.name in ("recall_my_context", "get_fleet_overview") or not c.env.entities or n >= 5:
                continue
            self.state.remember(user_id=req.user_id, conversation_id=run.conversation_id, topic=c.name, question=req.question, entity_ids=c.env.entities[:50],
                                window=c.env.window, tool_name=c.name)
            n += 1

    @staticmethod
    def _citation(e: EvidenceItem) -> Citation:
        fields = dict(list(e.fields.items())[:10])
        return Citation(ref=e.ref, type=e.type, id=e.id, title=e.title, kind=e.kind, path=e.path, fields=json.loads(json.dumps(fields, default=str)))

    def _history(self, conversation_id: str, user_id: str) -> list[Turn]:
        rows = self.state.history(conversation_id, user_id, limit=self.cfg.history_messages)
        turns = [Turn("user" if r["role"] == "user" else "model", text=r["content"]) for r in rows]
        while turns and turns[0].role != "user":
            turns.pop(0)
        return turns

    def _fail(self, run: _Run, code: str, message: str, thinking=None, retry_after: float | None = None) -> AskResponse:
        run.usage.seconds = round(time.perf_counter() - run.started, 3)
        try:
            self.state.audit(run.req.user_id, "ask_failed", "conversation", run.conversation_id or "", {"code": code, "model_calls": run.usage.model_calls, "tool_calls": run.usage.tool_calls})
        except Exception:                                            # pragma: no cover
            pass
        return AskResponse(ok=False, conversation_id=run.conversation_id, question=run.req.question, trace=run.trace, usage=run.usage, model=getattr(self.provider, "model", None),
                           thinking=thinking, pending_actions=run.pending, error=ErrorInfo(code=code, message=message[:500], retry_after_s=retry_after))
