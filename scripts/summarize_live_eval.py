#!/usr/bin/env python
"""Summarise saved live-evaluation reports (data/live_eval_*.json) into the QA matrix.

    python scripts/summarize_live_eval.py            # markdown table to stdout

For each question id the MOST RECENT recorded run is shown, together with how many runs there were and how many ended in a provider
failure (rate limit / outage) rather than a reasoning result. The reports themselves are git-ignored (they contain model output).
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_CODES = {"rate_limited", "llm_unavailable", "llm_not_configured", "llm_auth"}

# the official example questions (dataset/03-evaluation-queries.md) -> short topic
TOPICS = {"q1": "status card, one well", "q2": "wells not producing + reasons (table)", "q3": "13-week line chart with not-producing marks", "q4": "spill count/total (table), corrected excluded",
          "q5": "downtime in a year (hot-day trap)", "q6": "top-3 downtime (bar)", "q7": "spills in 90 days (map)", "q8": "spills area x month (heatmap)",
          "q9": "rules broken + open items across sources", "q10": "spills not contained in SOP time (report-derived)", "q11a": "ask, day 1 (setup for Q11)",
          "q11b": "memory: 'the three wells we discussed yesterday'", "q12a": "correct raw data (keeps original, flags disagreeing note)", "q12b": "Q2 again after the correction",
          "q13a": "correct AI analysis (4.5 -> 5.4 bbl) and its consequences", "q14a": "RCA candidates under the manual", "q14b": "team rule for a well type",
          "q14c": "RCA candidates under the team rule (scope)", "q15a": "correction reaches another user", "q15b": "which spills needed a regulator report (other user)",
          "q16a": "shared planned-downtime claim", "q16b": "another user asks about planned downtime", "q16c": "private memory is not leaked",
          "q17a": "claim by one user", "q17b": "conflicting claim by another user", "q17c": "claim contradicted by the latest inspection", "q18": "disagreement about the AI's spill detection"}


def main() -> int:
    runs: dict[str, list[dict]] = {}
    models = set()
    for f in sorted(glob.glob(str(ROOT / "data" / "live_eval_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        models.add(d.get("model"))
        for r in d["results"]:
            r["_file"] = Path(f).name
            runs.setdefault(r["id"], []).append(r)
    print(f"model(s): {', '.join(sorted(m for m in models if m))}; reports read: {len(glob.glob(str(ROOT / 'data' / 'live_eval_*.json')))}\n")
    print("| Question | Topic | Latest run | Hard checks | Soft checks | Tools | Visual | Time | Runs (provider failures) |")
    print("|---|---|---|---|---|---|---|---|---|")
    order = sorted(runs, key=lambda k: (int(re.match(r"q(\d+)", k).group(1)), k))
    for qid in order:
        rs = runs[qid]
        r = rs[-1]
        prov_fail = sum(1 for x in rs if (x.get("error") or {}).get("code") in PROVIDER_CODES if isinstance(x.get("error"), dict))
        err = r.get("error") if isinstance(r.get("error"), dict) else None
        hard = [c for c in r.get("checks", []) if not c["check"].startswith("~")]
        soft = [c for c in r.get("checks", []) if c["check"].startswith("~")]
        if err:
            status = f"**BLOCKED** ({err['code']})" if err["code"] in PROVIDER_CODES else f"**FAIL** ({err['code']})"
        else:
            status = "**PASS**" if r.get("ok") else "**FAIL**"
        hp = f"{sum(c['pass'] for c in hard)}/{len(hard)}" if hard else "-"
        sp = f"{sum(c['pass'] for c in soft)}/{len(soft)}" if soft else "-"
        failed = [c["check"] for c in hard if not c["pass"]]
        print(f"| {qid} | {TOPICS.get(qid, '')} | {status} | {hp}{' (failed: ' + '; '.join(failed) + ')' if failed else ''} | {sp} | {', '.join(r.get('tools') or []) or '-'} | {r.get('visual') or '-'} | {r.get('seconds')}s | {len(rs)} ({prov_fail}) |")
    # provider reliability, from the run logs
    counts: dict[str, int] = {}
    for f in glob.glob(str(ROOT / "data" / "live_*.log")):
        for m in re.finditer(r"gemma call failed \((\w+)\)", Path(f).read_text(encoding="utf-8", errors="replace")):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    print("\nProvider retries recorded in the run logs (each was retried; counts are failed attempts, not failed questions): " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "none"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
