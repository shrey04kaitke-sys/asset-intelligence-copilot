#!/usr/bin/env python
"""Live evaluation: runs the challenge's example questions through the real orchestrator with the REAL Gemma 4 API.

    python scripts/run_live_eval.py --list
    python scripts/run_live_eval.py --chains read            # Q1-Q10 style read questions
    python scripts/run_live_eval.py --chains memory,fix,rule,claims,dispute
    python scripts/run_live_eval.py --only q1,q2

Honest scope
* Checks are automated substring/structure assertions derived from dataset/03-evaluation-queries.md. They are NOT a grader
  and cannot judge wording quality; each result also stores the (truncated) answer for human review.
* Each chain runs in a throwaway state database, so corrections/claims never touch persistent state.
* The report is written to data/ (git-ignored). Nothing is mocked. API pacing is handled by the provider.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.config import ROOT, Settings  # noqa: E402
from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect, open_source  # noqa: E402
from copilot.domain import Domain  # noqa: E402
from copilot.feedback import Feedback  # noqa: E402
from copilot.llm import LLMError  # noqa: E402
from copilot.llm.gemma_api import GemmaApiProvider  # noqa: E402
from copilot.orchestrator import AskRequest, Orchestrator, OrchestratorConfig  # noqa: E402
from copilot.state import StateStore  # noqa: E402
from copilot.tools import build_registry  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MICHAEL, JESSICA, TYLER, JAKE, LAURA = "U-201", "U-202", "U-204", "U-102", "U-401"


def low(r) -> str:
    return (r.answer or "").lower()


def has(r, *needles, any_of=False) -> bool:
    t = low(r)
    hits = [n.lower() in t for n in needles]
    return any(hits) if any_of else all(hits)


def rows_ids(r, key="asset_id") -> set[str]:
    return {x.get(key) for x in (r.visual or {}).get("rows", [])} if (r.visual or {}).get("type") == "table" else set()


def cites(r, *refs) -> bool:
    got = {c.ref for c in r.citations}
    return all(x in got for x in refs)


class Ctx:
    def __init__(self, provider, settings):
        self.provider, self.settings = provider, settings
        self.src = open_source(settings)
        self.results: list[dict] = []
        self.new_chain()

    def new_chain(self):
        d = Path(tempfile.mkdtemp(prefix="copilot-live-eval-"))
        self.state = StateStore(connect(d / "state.db", STATE_DDL, STATE_SCHEMA_VERSION))
        self.dom = Domain(self.src, self.state)
        self.fb = Feedback(self.dom)
        self.orch = Orchestrator(self.dom, self.state, self.fb, build_registry(), self.provider, OrchestratorConfig())

    def ask(self, qid: str, user: str, question: str, checks: list[tuple[str, callable]], **kw):
        t0 = time.perf_counter()
        print(f"\n[{qid}] {question[:110]}", flush=True)
        try:
            r = self.orch.ask(AskRequest(user_id=user, question=question, **kw))
        except LLMError as e:
            r = None
            print(f"   LLM ERROR {e.kind}: {e.message[:200]}", flush=True)
        rec = {"id": qid, "user": user, "question": question, "seconds": round(time.perf_counter() - t0, 1), "checks": [], "ok": False}
        if r is None or not r.ok:
            rec["error"] = (r.error.model_dump() if r is not None and r.error else "provider error")
            rec["trace"] = [t.model_dump() for t in r.trace] if r is not None else []
            print(f"   FAILED: {rec['error']}", flush=True)
        else:
            rec.update(answer=(r.answer or "")[:900], tools=[t.name for t in r.trace if t.kind == "tool"], model_calls=r.usage.model_calls,
                       tokens={"prompt": r.usage.prompt_tokens, "output": r.usage.output_tokens, "thought": r.usage.thought_tokens},
                       visual=(r.visual or {}).get("type"), citations=[c.ref for c in r.citations][:12], warnings=r.warnings, uncertainty=r.uncertainty,
                       conflicts=r.conflicts, needs_confirmation=r.needs_confirmation)
            for label, fn in checks:
                try:
                    ok = bool(fn(r))
                except Exception as e:                       # a broken check counts as failed, loudly
                    ok = False
                    label += f" [check error: {type(e).__name__}]"
                rec["checks"].append({"check": label, "pass": ok})
            rec["ok"] = all(c["pass"] for c in rec["checks"]) and bool(rec["checks"])
            print(f"   tools={rec['tools']} model_calls={r.usage.model_calls} visual={rec['visual']} {rec['seconds']}s", flush=True)
            print(f"   answer: {(r.answer or '')[:260]!r}", flush=True)
            for c in rec["checks"]:
                print(f"   [{'ok' if c['pass'] else 'XX'}] {c['check']}", flush=True)
            if r.warnings:
                print(f"   warnings: {r.warnings}", flush=True)
        self.results.append(rec)
        return r


# ------------------------------------------------------------------------------------------ chains
def chain_read(c: Ctx, only):
    def run(qid, *a, **k):
        if only is None or qid in only:
            return c.ask(qid, *a, **k)
    run("q1", JESSICA, "Is WELL-0001 producing oil right now? When was it last inspected, and by whom? Show me its status card.", [
        ("says it is NOT producing", lambda r: has(r, "not producing", "no,", "isn't producing", "is not producing", any_of=True)),
        ("names the inspector Ryan Brooks", lambda r: has(r, "ryan brooks")),
        ("status card shows 3.2 °C above ambient", lambda r: any(f.get("label") == "Wellhead above air" and f.get("value") == 3.2 for f in (r.visual or {}).get("fields", []))),
        ("inspection date 15 Oct / INS-0001-0203", lambda r: has(r, "15 oct", "2026-10-15", "october 15", "ins-0001-0203", any_of=True)),
        ("explains via the planned shut-in note", lambda r: has(r, "shut", "rod string", "ctx-0001-003", any_of=True)),
        ("flags the out-of-date recorded status", lambda r: any("recorded_status" in x or "out of date" in x for x in r.conflicts) or has(r, "out of date", "stale", "outdated", any_of=True)),
        ("status card visual", lambda r: (r.visual or {}).get("type") == "card"),
        ("cites the inspection", lambda r: cites(r, "inspection:INS-0001-0203"))])
    run("q2", MICHAEL, "Which wells in Northeast Block 8 are not producing oil right now, and why? Show them in a table.", [
        ("table of exactly the 4 expected wells", lambda r: rows_ids(r) == {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"}),
        ("says WELL-0164 and WELL-0175 have no recorded reason", lambda r: has(r, "0164", "0175") and has(r, "no recorded reason", "no reason", "not recorded", "no explanation", any_of=True)),
        ("names the open repairs MNT-0170-031 / MNT-0172-028", lambda r: has(r, "mnt-0170-031") and has(r, "mnt-0172-028")),
        ("backend flags the 2 unexplained wells", lambda r: any("2 not-producing" in u for u in r.uncertainty))])
    run("q3", MICHAEL, "Show a line chart of WELL-0001's wellhead temperature against air temperature for the last 13 weeks, and mark when it was not producing oil.", [
        ("line chart with 13 points", lambda r: (r.visual or {}).get("type") == "line" and len(r.visual["series"][0]["points"]) == 13),
        ("marks 8 Oct and 15 Oct", lambda r: [m["x"] for m in r.visual["markers"]] == ["2026-10-08", "2026-10-15"])])
    run("q4", MICHAEL, "How many oil spills has WELL-0001 had in the last 4 years, and how much oil in total? List them in a table.", [
        ("6 spills", lambda r: has(r, "6", "six", any_of=True)), ("15.1 barrels", lambda r: has(r, "15.1")),
        ("table with 6 rows", lambda r: (r.visual or {}).get("type") == "table" and len([x for x in r.visual["rows"] if "counted" in str(x.get("status"))]) == 6),
        ("mentions the corrected 7 Mar 2024 detection", lambda r: has(r, "ins-0001-0070", "rainwater", "7 mar", "corrected", any_of=True))])
    run("q5", MICHAEL, "How many days was WELL-0001 down in 2025?", [
        ("21 days", lambda r: has(r, "21")), ("cites inspections", lambda r: len(r.citations) >= 1),
        ("mentions the hot 24 Jul reading or the dates", lambda r: has(r, "24 jul", "2025-07-24", "july 24", "8.5", any_of=True))])
    run("q6", MICHAEL, "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart.", [
        ("bar chart", lambda r: (r.visual or {}).get("type") == "bar"),
        ("exactly WELL-0121, WELL-0138, WELL-0107", lambda r: set(r.visual["categories"]) == {"WELL-0121", "WELL-0138", "WELL-0107"}),
        ("values 49 / 42 / 42", lambda r: sorted(r.visual["series"][0]["values"]) == [42, 42, 49]), ("answer states 49", lambda r: has(r, "49"))])
    run("q7", MICHAEL, "Show on a map every well in Northeast Block 8 with a real spill in the last 90 days.", [
        ("map visual", lambda r: (r.visual or {}).get("type") == "map"),
        ("4 pins: 0151, 0165, 0175, 0186", lambda r: {p["label"] for p in r.visual["points"]} == {"WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"})])
    run("q8", MICHAEL, "Show a heatmap of 2026 spills by geographic area and month.", [
        ("heatmap", lambda r: (r.visual or {}).get("type") == "heatmap"),
        ("row totals 35/43/37/26", lambda r: dict(zip(r.visual["row_labels"], r.visual["row_totals"])) == {"Southwest Block 23": 35, "Southwest Block 24": 43, "Northeast Block 7": 37, "Northeast Block 8": 26})])
    run("q9", MICHAEL, "Which rules has WELL-0001 broken in the last 12 months, and what is still open?", [
        ("lists SR-01, SR-02, SR-03 and RC-04", lambda r: has(r, "sr-01", "sr-02", "sr-03", "rc-04")),
        ("41 hours vs 24-hour limit", lambda r: has(r, "41") and has(r, "24")), ("USD 5,000 fine open", lambda r: has(r, "5,000", "5000", any_of=True)),
        ("root-cause analysis pending", lambda r: has(r, "root-cause", "root cause", "rca", any_of=True)),
        ("planned shut-in not in the maintenance system", lambda r: has(r, "maintenance system", "not recorded", "not yet recorded", "work order", any_of=True))])
    run("q10", JESSICA, "In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?", [
        ("WELL-0082 and WELL-0100", lambda r: has(r, "0082", "0100")), ("7.1 and 8.1 hours", lambda r: has(r, "7.1", "8.1")),
        ("no other well flagged", lambda r: not has(r, "0080", "0085", "0073", "0098", "0091", "0097", "0077", any_of=True))])


def chain_memory(c: Ctx, only):
    c.new_chain()
    q = "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart."
    c.ask("q11a", MICHAEL, q, [("top 3 found", lambda r: set(r.visual["categories"]) == {"WELL-0121", "WELL-0138", "WELL-0107"})])
    r = c.ask("q11b", MICHAEL, "For the three wells we discussed yesterday, what have repairs cost this year?", [
        ("uses recall", lambda r: "recall_my_context" in [t.name for t in r.trace]),
        ("25,794 for WELL-0121", lambda r: has(r, "25,794", "25794", any_of=True)), ("46,942 for WELL-0138", lambda r: has(r, "46,942", "46942", any_of=True)),
        ("5,176 for WELL-0107", lambda r: has(r, "5,176", "5176", any_of=True))])
    c.ask("q16c", JESSICA, "For the three wells we discussed yesterday, what have repairs cost this year?", [
        ("says there is no earlier conversation", lambda r: has(r, "no earlier", "no previous", "don't have", "do not have", "no record", "no such", "not have", any_of=True)),
        ("does NOT reveal Michael's wells or costs", lambda r: not has(r, "25,794", "46,942", "5,176", "0121", "0138", "0107", any_of=True))])


def chain_fix(c: Ctx, only):
    c.new_chain()
    r = c.ask("q12a", MICHAEL, "The 15 Oct reading for WELL-0164 is wrong. The handheld thermal gun used in that inspection was faulty; the field crew re-measured the wellhead at 41.0 °C.", [
        ("correction recorded for INS-0164-0205 by Michael", lambda r: any(x["target_id"] == "INS-0164-0205" and x["author_user_id"] == MICHAEL and x["new_value"] == 41.0 and x["original_value"] == 24.0 for x in c.state.corrections())),
        ("says the original 24.0 is kept", lambda r: has(r, "24")), ("now producing", lambda r: has(r, "producing")),
        ("raises the 'Pump jack not moving' disagreement and asks to confirm", lambda r: has(r, "pump jack not moving") and has(r, "confirm")),
        ("backend lists the confirmation need", lambda r: any("Pump jack not moving" in x for x in r.needs_confirmation))])
    c.ask("q12b", MICHAEL, "Which wells in Northeast Block 8 are not producing oil right now, and why? Show them in a table.", [
        ("three wells, WELL-0164 gone", lambda r: rows_ids(r) == {"WELL-0170", "WELL-0172", "WELL-0175"}), ("explains why 0164 is not listed", lambda r: has(r, "0164"))])
    c.ask("q13a", MICHAEL, "The AI estimated the 24 Sep spill at WELL-0151 as 4.5 bbl. The field crew measured 5.4 bbl.", [
        ("correction recorded 4.5 -> 5.4", lambda r: any(x["target_id"] == "INS-0151-0201" and x["new_value"] == 5.4 and x["original_value"] == 4.5 for x in c.state.corrections())),
        ("reportable", lambda r: has(r, "reportable")), ("possible SR-01 breach / no notice", lambda r: has(r, "sr-01") and has(r, "no", "overdue", "not recorded", any_of=True)),
        ("SR-03 due 23 Nov", lambda r: has(r, "23 nov", "2026-11-23", "november 23", any_of=True)), ("report MNT-0151-025 out of date / 6.4 h", lambda r: has(r, "mnt-0151-025", "6.4", any_of=True))])
    c.ask("q15a", JESSICA, "Is WELL-0164 producing oil right now?", [
        ("yes, producing", lambda r: has(r, "yes", "is producing", "producing oil", any_of=True) and not has(r, "not producing", "isn't producing")),
        ("attributes the correction to Michael Turner", lambda r: has(r, "michael turner")), ("41.0 °C", lambda r: has(r, "41")),
        ("mentions the inspector's note", lambda r: has(r, "pump jack not moving"))])
    c.ask("q15b", LAURA, "Which spills in Northeast Block 8 in the last 90 days needed a report to the regulator?", [
        ("WELL-0151 only", lambda r: has(r, "0151") and not has(r, "0165", "0175", "0186", any_of=True)), ("5.4", lambda r: has(r, "5.4")),
        ("flagged overdue / no notice on record", lambda r: has(r, "overdue", "no written notice", "not recorded", "no notice", "none is recorded", "no record", any_of=True))])


def chain_rule(c: Ctx, only):
    c.new_chain()
    c.ask("q14a", MICHAEL, "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?", [
        ("exactly 0108, 0121, 0138", lambda r: has(r, "0108", "0121", "0138") and not has(r, "0120", "0102", any_of=True))])
    c.ask("q14b", MICHAEL, "Note for all T350 wells: our team now wants a pump jack root-cause analysis after 2 repairs, not 3.", [
        ("rule recorded for T350 only", lambda r: any(x["kind"] == "rule_override" and x["target_id"] == "T350" and x["new_value"] == 2 for x in c.state.corrections(kind="rule_override")))])
    c.ask("q14c", MICHAEL, "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?", [
        ("0108, 0120, 0121, 0138", lambda r: has(r, "0108", "0120", "0121", "0138")), ("0102 (T200) not added", lambda r: not has(r, "0102")),
        ("says this follows Michael's rule, not the manual", lambda r: has(r, "michael", "your rule", "team", "user", any_of=True))])


def chain_claims(c: Ctx, only):
    c.new_chain()
    c.ask("q16a", JAKE, "FYI for everyone: WELL-0007 will be shut in from 17 Oct for a 10-day planned workover.", [
        ("shared planned-shutdown claim recorded", lambda r: any(x["asset_id"] == "WELL-0007" and x["author_user_id"] == JAKE and x["visibility"] == "shared" for x in c.state.claims("WELL-0007", MICHAEL)))])
    c.ask("q16b", MICHAEL, "Is any downtime planned for WELL-0007?", [
        ("yes, from 17 Oct", lambda r: has(r, "17 oct", "2026-10-17", "october 17", any_of=True)), ("attributed to Jake Morales", lambda r: has(r, "jake morales")),
        ("about 10 days", lambda r: has(r, "10", "27 oct", "2026-10-27", any_of=True))])
    c.ask("q17a", MICHAEL, "WELL-0175 has been shut in for planned work since 14 Oct.", [("recorded", lambda r: len(c.state.claims("WELL-0175", MICHAEL)) == 1)])
    c.ask("q17b", TYLER, "WELL-0175 is running fine.", [
        ("tells Tyler the 15 Oct inspection shows 0.4 °C above air / not producing", lambda r: has(r, "0.4") and has(r, "not producing", "isn't producing", "no", any_of=True)),
        ("keeps both statements and marks Tyler's unconfirmed", lambda r: has(r, "unconfirmed", "not confirmed", "cannot confirm", "contradict", "conflict", any_of=True)),
        ("asks for confirmation", lambda r: has(r, "confirm")), ("both claims stay on record", lambda r: len(c.state.claims("WELL-0175", MICHAEL)) == 2)])
    c.ask("q17c", JAKE, "WELL-0001 is back online since 14 Oct.", [
        ("points to the 15 Oct inspection (not producing)", lambda r: has(r, "not producing", "isn't producing", "not back", "no,", any_of=True)),
        ("mentions the ~20 Oct expected return / CTX-0001-003", lambda r: has(r, "20 oct", "2026-10-20", "october 20", "ctx-0001-003", any_of=True)), ("unconfirmed", lambda r: has(r, "unconfirmed", "not confirmed", "confirm", any_of=True))])


def chain_dispute(c: Ctx, only):
    c.new_chain()
    c.ask("q18", MICHAEL, "Was the 20 Aug spill at WELL-0101 real oil?", [
        ("answers yes", lambda r: has(r, "yes", "real oil", "crude", any_of=True)), ("shows the disagreement: Ryan Brooks vs Chris Dalton", lambda r: has(r, "ryan brooks") and has(r, "chris dalton")),
        ("cites INS-0101-0197 and both notes", lambda r: cites(r, "inspection:INS-0101-0197") and cites(r, "note:CTX-0101-002", "note:CTX-0101-003")),
        ("detection stands / repair done", lambda r: has(r, "mnt-0101-029", "repair", "stands", "no formal correction", "not a formal correction", any_of=True))])


CHAINS = {"read": chain_read, "memory": chain_memory, "fix": chain_fix, "rule": chain_rule, "claims": chain_claims, "dispute": chain_dispute}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="")
    ap.add_argument("--only", default="", help="comma list of question ids inside the 'read' chain, e.g. q1,q2")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--thinking", choices=["minimal", "high"], default=None)
    a = ap.parse_args()
    if a.list:
        print("chains:", ", ".join(CHAINS), "| read-chain ids: q1..q10")
        return 0
    chains = [x for x in a.chains.split(",") if x] or (["read"] if a.only else [])
    only = {x for x in a.only.split(",") if x} or None
    if not chains:
        print("choose --chains (see --list) or --only")
        return 2
    settings = Settings()
    try:
        provider = GemmaApiProvider(settings)
    except LLMError as e:
        print("NOT RUN:", e)
        return 2
    ctx = Ctx(provider, settings)
    if a.thinking:
        ctx.orch.cfg.thinking = a.thinking
    print(f"model={provider.model} chains={chains} only={sorted(only) if only else 'all'}", flush=True)
    for name in chains:
        CHAINS[name](ctx, only)
    ok = sum(1 for r in ctx.results if r["ok"])
    tot_tokens = sum(sum((r.get("tokens") or {}).values()) for r in ctx.results)
    calls = sum(r.get("model_calls", 0) for r in ctx.results)
    print(f"\n==== {ok}/{len(ctx.results)} questions passed all automated checks; model calls={calls}; total tokens ~{tot_tokens}")
    out = ROOT / "data" / f"live_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": provider.model, "chains": chains, "results": ctx.results}, indent=1, default=str), encoding="utf-8")
    print("report:", out.relative_to(ROOT))
    return 0 if ok == len(ctx.results) else 1


if __name__ == "__main__":
    sys.exit(main())
