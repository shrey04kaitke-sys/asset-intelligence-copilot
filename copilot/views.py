"""Read models for the workspace UI.

Everything here is assembled from the same deterministic domain functions and registry tools the chat uses, so a number
shown on a dashboard card and a number in a chat answer cannot disagree. Nothing is hardcoded and nothing is estimated by a
model. Private data (claims marked private, a user's own investigations) is returned only to its owner.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import timedelta
from typing import Any

from .domain import INS_REF, Domain
from .feedback import Feedback
from .timewin import TimeWindow
from .tools import ToolContext, ToolRegistry

PERIODS = {"30d": TimeWindow(last_n=30, unit="days"), "90d": TimeWindow(last_n=90, unit="days"), "12m": TimeWindow(last_n=12, unit="months"),
           "ytd": TimeWindow(), "all": TimeWindow()}
PERIOD_LABEL = {"30d": "last 30 days", "90d": "last 90 days", "12m": "last 12 months", "ytd": "year to date", "all": "all history"}
SORTS = {"id", "delta", "inspected", "area", "status"}


class ViewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Views:
    def __init__(self, domain: Domain, registry: ToolRegistry, feedback: Feedback):
        self.d, self.reg, self.fb = domain, registry, feedback
        self.state = domain.state

    # ------------------------------------------------------------------ helpers
    def _ctx(self, user_id: str | None) -> ToolContext:
        return ToolContext(self.d, self.fb, user_id=user_id, allow_writes=False)

    def _tool(self, name: str, args: dict, user_id: str | None) -> dict:
        env = self.reg.call(name, args, self._ctx(user_id))
        out = env.model_dump()
        return out

    def _window(self, period: str) -> TimeWindow:
        if period not in PERIODS:
            raise ViewError(422, f"period must be one of {sorted(PERIODS)}")
        if period == "ytd":
            return TimeWindow(calendar_year=self.d.as_of.year)
        return PERIODS[period]

    def _area_filter(self, area: str | None, well_type: str | None) -> tuple[str | None, str | None]:
        a = None
        if area:
            r = self.d.resolve_area(area)
            if not r:
                raise ViewError(404, f"unknown area '{area[:40]}'")
            a = r["area_id"]
        if well_type and well_type.upper() not in self.d.well_types():
            raise ViewError(404, f"unknown well type '{well_type[:20]}'")
        return a, well_type.upper() if well_type else None

    # ------------------------------------------------------------------ fleet overview
    def fleet(self, *, area: str | None = None, well_type: str | None = None, period: str = "90d", user_id: str | None = None) -> dict:
        d = self.d
        area_id, wt = self._area_filter(area, well_type)
        w = self._window(period)
        assets = d.assets(area=area_id, well_type=wt)
        ids = [a["asset_id"] for a in assets]
        by_id = {a["asset_id"]: a for a in assets}
        areas = d.areas()
        f = d.fleet_status(asset_ids=ids, viewer=user_id) if ids else {"rows": [], "evidence": []}
        rows = f["rows"]
        known = {r["asset_id"] for r in rows}
        n_prod = sum(1 for r in rows if r["producing"] is True)
        n_off = sum(1 for r in rows if r["producing"] is False)
        n_unknown = len(ids) - n_prod - n_off
        n_stale = sum(1 for r in rows if r["stale_recorded_status"])
        n_noreason = sum(1 for r in rows if r.get("no_recorded_reason"))
        drill = lambda **p: {"route": "assets", "params": {k: v for k, v in {"area": area_id, "well_type": wt, **p}.items() if v}}
        totals = [
            {"key": "wells", "label": "Wells", "value": len(ids), "drill": drill()},
            {"key": "producing", "label": "Producing now", "value": n_prod, "drill": drill(status="producing")},
            {"key": "not_producing", "label": "Not producing now", "value": n_off, "drill": drill(status="not_producing")},
            {"key": "no_reason", "label": "Not producing, no recorded reason", "value": n_noreason, "drill": drill(status="not_producing", reason="none")},
            {"key": "stale", "label": "Recorded status out of date", "value": n_stale, "drill": drill(stale="1")},
        ]
        if n_unknown:
            totals.append({"key": "unknown", "label": "No usable inspection", "value": n_unknown, "drill": drill(status="unknown")})
        # period-based indicators
        sp = d.spills(ids, w) if ids else {"rows": [], "count": 0, "total_bbl": 0.0}
        spills_by_area: dict[str, int] = defaultdict(int)
        for r in sp["rows"]:
            if not r["voided"]:
                spills_by_area[r["area"]] += 1
        dt = d.downtime(ids, w, user_id) if ids else {"rows": []}
        down_by_area: dict[str, int] = defaultdict(int)
        for r in dt["rows"]:
            down_by_area[r["area"]] += r["downtime_days"]
        mt = d.maintenance(ids, w) if ids else {"rows": [], "total_cost_usd": 0, "count": 0}
        period_stats = [
            {"key": "spills", "label": f"Spills ({PERIOD_LABEL[period]})", "value": sp["count"], "unit": "spills", "note": f"{sp['total_bbl']:g} bbl; corrected detections excluded"},
            {"key": "downtime", "label": f"Downtime ({PERIOD_LABEL[period]})", "value": sum(down_by_area.values()), "unit": "days", "note": "7 days per weekly inspection showing the well not producing"},
            {"key": "repairs", "label": f"Repairs started ({PERIOD_LABEL[period]})", "value": mt["count"], "unit": "repairs", "note": f"USD {mt['total_cost_usd']:,.0f}"},
        ]
        by_area = []
        for aid, a in areas.items():
            if area_id and aid != area_id:
                continue
            mine = [r for r in rows if r["area"] == aid]
            by_area.append({"area_id": aid, "name": a["name"], "wells": sum(1 for x in assets if x["area_id"] == aid), "producing": sum(1 for r in mine if r["producing"] is True),
                            "not_producing": sum(1 for r in mine if r["producing"] is False), "spills": spills_by_area.get(aid, 0), "downtime_days": down_by_area.get(aid, 0)})
        by_type = []
        for t in d.well_types():
            if wt and t != wt:
                continue
            mine = [r for r in rows if r["well_type"] == t]
            by_type.append({"well_type": t, "wells": sum(1 for x in assets if x["well_type"] == t), "producing": sum(1 for r in mine if r["producing"] is True),
                            "not_producing": sum(1 for r in mine if r["producing"] is False)})
        # compliance concerns (all from recorded evidence)
        reg = d.src.execute("SELECT event_type, status, fine_usd, asset_id FROM reg_events").fetchall()
        scope = set(ids)
        open_nov = [r for r in reg if r["event_type"] == "notice_of_violation" and r["status"] == "open" and r["asset_id"] in scope]
        open_fines = sum(r["fine_usd"] or 0 for r in open_nov)
        rca = d.rca_status(ids) if ids else {"rows": []}
        rca_n = sum(1 for r in rca["rows"] if r["rca_required"])
        year = d.spills(ids, PERIODS["12m"]) if ids else {"rows": []}
        sr = defaultdict(int)
        for s in year["rows"]:
            if s["voided"]:
                continue
            for k in d.spill_compliance(s)["checks"]:
                if k["verdict"] in ("breached", "no_report_on_record_overdue"):
                    sr[k["protocol"]] += 1
        compliance = [
            {"key": "open_nov", "label": "Open notices of violation", "value": len(open_nov), "note": f"USD {open_fines:,.0f} in open fines", "drill": drill(concern="open_nov")},
            {"key": "rca", "label": "Root-cause analyses required", "value": rca_n, "note": "corrective repairs reached the manual's threshold", "drill": drill(concern="rca")},
            {"key": "sr01", "label": "Late/missing spill reports (SR-01), 12 months", "value": sr.get("SR-01", 0), "drill": drill(concern="sr01")},
            {"key": "sr02", "label": "Spills not contained in time (SR-02), 12 months", "value": sr.get("SR-02", 0), "drill": drill(concern="sr02")},
            {"key": "sr03", "label": "Overdue clean-up verification (SR-03), 12 months", "value": sr.get("SR-03", 0), "drill": drill(concern="sr03")},
        ]
        # wells that deserve a look, with the reason stated
        attention = []
        nov_assets = {r["asset_id"] for r in open_nov}
        for r in rows:
            reasons, sev = [], 0
            if r["producing"] is False and r.get("no_recorded_reason"):
                reasons.append("not producing and no recorded reason" + (f" (inspector: \"{r['inspector_observation']}\")" if r.get("inspector_observation") else ""))
                sev = 3
            elif r["producing"] is False:
                reasons.append("not producing: " + (r["reasons"][0] if r.get("reasons") else "see detail"))
                sev = 2
            if r["stale_recorded_status"]:
                reasons.append(f"recorded status '{r['recorded_status']}' is out of date")
                sev = max(sev, 1)
            if r["asset_id"] in nov_assets:
                reasons.append("open notice of violation")
                sev = max(sev, 3)
            if reasons:
                attention.append({"asset_id": r["asset_id"], "area": areas[r["area"]]["name"] if r["area"] in areas else r["area"], "severity": ["info", "info", "medium", "high"][sev], "reasons": reasons,
                                  "ref": f"inspection:{r['inspection_id']}", "_sev": sev})
        attention.sort(key=lambda x: (-x["_sev"], x["asset_id"]))
        for x in attention:
            x.pop("_sev")
        events = self._recent_events(ids, scope, user_id)
        return {"as_of": d.as_of.isoformat(), "basis": d.as_of_basis, "last_ingest_at": d.last_ingest_at, "period": period, "period_label": PERIOD_LABEL[period],
                "filters": {"area": area_id, "well_type": wt}, "totals": totals, "period_stats": period_stats, "status_distribution": [
                    {"label": "Producing", "value": n_prod}, {"label": "Not producing", "value": n_off}] + ([{"label": "No usable inspection", "value": n_unknown}] if n_unknown else []),
                "by_area": by_area, "by_type": by_type, "compliance": compliance, "attention": attention[:25], "attention_total": len(attention), "recent_events": events,
                "areas": [{"area_id": k, "name": v["name"]} for k, v in areas.items()], "well_types": d.well_types(),
                "rule_problems": {"conflicts": d.rule_conflicts(), "rejected": d.rule_rejections()},
                "note": "Status comes from each well's latest weekly inspection and its well-type rule; the dataset is a snapshot, not a live feed."}

    def _recent_events(self, ids: list[str], scope: set[str], user_id: str | None, limit: int = 14) -> list[dict]:
        d = self.d
        ev: list[dict] = []
        if not ids:
            return ev
        cutoff = (d.as_of - timedelta(days=45)).isoformat()
        for r in d.src.execute("SELECT * FROM reg_events WHERE date>=? ORDER BY date DESC LIMIT 60", (cutoff,)):
            if r["asset_id"] in scope:
                ev.append({"date": r["date"], "kind": "regulatory", "asset_id": r["asset_id"], "label": f"{r['event_type'].replace('_', ' ')} ({r['status']})", "detail": r["detail"], "ref": f"reg_event:{r['event_id']}"})
        for r in d.src.execute("SELECT * FROM maintenance WHERE started_at>=? ORDER BY started_at DESC LIMIT 80", (cutoff + "T00:00:00Z",)):
            if r["asset_id"] in scope:
                ev.append({"date": r["started_at"][:10], "kind": "repair", "asset_id": r["asset_id"], "label": f"{r['maintenance_type']} repair: {r['issue']}", "detail": f"{r['equipment_kind']}, USD {r['cost_total']:,.0f}", "ref": f"maintenance:{r['maintenance_id']}"})
        sp = d.spills([i for i in ids], TimeWindow(start_date=d.as_of - timedelta(days=45), end_date=d.as_of))
        for s in sp["rows"]:
            if not s["voided"]:
                ev.append({"date": s["date"], "kind": "spill", "asset_id": s["asset_id"], "label": f"spill {s['volume_bbl']:g} bbl ({s['class']})", "detail": s["inspector_note"] or "", "ref": f"inspection:{s['inspection_id']}"})
        for c in self.state.corrections(active_only=False)[-10:]:
            ev.append({"date": c["created_at"][:10], "kind": "correction", "asset_id": c["target_id"] if c["target_type"] == "asset" else None,
                       "label": f"{c['kind'].replace('_', ' ')} by {d.user_name(c['author_user_id'])} ({c['status']})", "detail": c["reason"] or "", "ref": f"correction:{c['correction_id']}"})
        ev.sort(key=lambda e: e["date"], reverse=True)
        return ev[:limit]

    # ------------------------------------------------------------------ asset list
    def assets(self, *, area=None, well_type=None, status="all", stale=False, reason=None, concern=None, q=None, sort="id", desc=False, limit=50, offset=0, user_id=None) -> dict:
        d = self.d
        if status not in ("all", "producing", "not_producing", "unknown"):
            raise ViewError(422, "status must be all, producing, not_producing or unknown")
        if sort not in SORTS:
            raise ViewError(422, f"sort must be one of {sorted(SORTS)}")
        area_id, wt = self._area_filter(area, well_type)
        assets = d.assets(area=area_id, well_type=wt)
        ids = [a["asset_id"] for a in assets]
        meta = {a["asset_id"]: a for a in assets}
        f = d.fleet_status(asset_ids=ids, viewer=user_id) if ids else {"rows": []}
        stat = {r["asset_id"]: r for r in f["rows"]}
        concern_ids: set[str] | None = None
        if concern:
            concern_ids = self._concern_assets(concern, ids)
        out = []
        for aid in ids:
            a, r = meta[aid], stat.get(aid)
            producing = r["producing"] if r else None
            st = "producing" if producing is True else ("not_producing" if producing is False else "unknown")
            if status != "all" and st != status:
                continue
            if stale and not (r and r["stale_recorded_status"]):
                continue
            if reason == "none" and not (r and r.get("no_recorded_reason")):
                continue
            if concern_ids is not None and aid not in concern_ids:
                continue
            if q and q.lower() not in f"{aid} {a['name']}".lower():
                continue
            out.append({"asset_id": aid, "name": a["name"], "area_id": a["area_id"], "area": d.areas().get(a["area_id"], {}).get("name", a["area_id"]), "well_type": a["well_type"], "status": st,
                        "delta_c": r["delta_c"] if r else None, "threshold_c": r["threshold_c"] if r else None, "recorded_status": a["recorded_status"],
                        "recorded_status_stale": bool(r and r["stale_recorded_status"]), "last_inspected": r["inspected_at"] if r else None,
                        "reason": ("; ".join(r.get("reasons") or []) or "No recorded reason") if (r and producing is False) else "",
                        "inspector_observation": (r or {}).get("inspector_observation"), "ref": f"inspection:{r['inspection_id']}" if r else None})
        keyf = {"id": lambda x: x["asset_id"], "delta": lambda x: (x["delta_c"] is None, x["delta_c"] if x["delta_c"] is not None else 0), "inspected": lambda x: x["last_inspected"] or "",
                "area": lambda x: (x["area"], x["asset_id"]), "status": lambda x: (x["status"], x["asset_id"])}[sort]
        out.sort(key=keyf, reverse=desc)
        total = len(out)
        limit = max(1, min(int(limit), 200))
        return {"total": total, "offset": offset, "limit": limit, "rows": out[offset: offset + limit], "as_of": d.as_of.isoformat(), "filters": {"area": area_id, "well_type": wt, "status": status, "stale": stale, "reason": reason, "concern": concern, "q": q}}

    def _concern_assets(self, concern: str, ids: list[str]) -> set[str]:
        d = self.d
        s = set(ids)
        if concern == "open_nov":
            return {r["asset_id"] for r in d.src.execute("SELECT asset_id FROM reg_events WHERE event_type='notice_of_violation' AND status='open'") if r["asset_id"] in s}
        if concern == "rca":
            return {r["asset_id"] for r in d.rca_status(ids)["rows"] if r["rca_required"]}
        if concern in ("sr01", "sr02", "sr03"):
            proto = concern.upper().replace("SR", "SR-")
            out = set()
            for sp in d.spills(ids, PERIODS["12m"])["rows"]:
                if sp["voided"]:
                    continue
                if any(k["protocol"] == proto and k["verdict"] in ("breached", "no_report_on_record_overdue") for k in d.spill_compliance(sp)["checks"]):
                    out.add(sp["asset_id"])
            return out
        raise ViewError(422, "unknown concern filter")

    # ------------------------------------------------------------------ one asset
    def asset(self, asset_id: str, user_id: str | None = None) -> dict:
        d = self.d
        a = d.asset(asset_id)
        if not a:
            raise ViewError(404, f"asset {asset_id[:30]} not found")
        aid = a["asset_id"]
        equip = [dict(r) for r in d.src.execute("SELECT equipment_id, kind, model FROM equipment WHERE asset_id=?", (aid,))]
        status = self._tool("get_asset_status", {"asset_id": aid}, user_id)
        series = self._tool("get_inspection_series", {"asset_id": aid, "window": {"last_n": 52, "unit": "weeks"}}, user_id)
        timeline = self._tool("get_asset_timeline", {"asset_id": aid, "window": {"last_n": 12, "unit": "months"}}, user_id)
        compliance = self._tool("get_asset_compliance", {"asset_id": aid, "window": {"last_n": 12, "unit": "months"}}, user_id)
        mnt = d.maintenance([aid], TimeWindow())
        sp = d.spills([aid], TimeWindow(), include_voided=True)
        spills = []
        for s in reversed(sp["rows"]):
            row = {k: s[k] for k in ("inspection_id", "date", "volume_bbl", "original_volume_bbl", "area_pct", "class", "voided", "void_reason", "void_source", "repairs", "regulatory_events", "inspector_note")}
            row["informal_notes"] = [{"note_id": n["note_id"], "author": n["author_name"], "text": n["text"]} for n in s["informal_notes"]]
            if not s["voided"]:
                row["checks"] = [{"protocol": k["protocol"], "verdict": k["verdict"], "detail": k["detail"], "refs": k.get("refs", [])} for k in d.spill_compliance(s)["checks"]]
            spills.append(row)
        reg = d.reg_events([aid], TimeWindow())
        notes = d.notes(aid, user_id)
        insp_ids = [r[0] for r in d.src.execute("SELECT inspection_id FROM inspections WHERE asset_id=?", (aid,))]
        corrections = self._corrections_for(aid, a["well_type"], insp_ids)
        invest = []
        if user_id and user_id in d.users():
            for m in self.state.recall(user_id, limit=300):
                if aid in m["entity_ids"]:
                    invest.append({"when": m["created_at"], "question": m["question"], "conversation_id": m["conversation_id"], "tool": m["tool_name"]})
        return {
            "asset": {"asset_id": aid, "name": a["name"], "well_type": a["well_type"], "area_id": a["area_id"], "area": d.areas().get(a["area_id"], {}).get("name"), "team_id": a["team_id"],
                      "in_charge": d.user_name(a["in_charge_user_id"]), "installed_on": a["installed_on"], "lat": a["lat"], "lon": a["lon"], "recorded_status": a["recorded_status"],
                      "equipment": equip, "source": a["src_path"], "ref": f"asset:{aid}"},
            "status": status, "series": series, "timeline": timeline, "compliance": compliance,
            "maintenance": [{**r, "ref": f"maintenance:{r['maintenance_id']}", "report_ref": f"report:{r['maintenance_id']}" if r["report_file"] else None} for r in reversed(mnt["rows"])],
            "maintenance_total_cost_usd": mnt["total_cost_usd"], "spills": spills, "spill_totals": {"count": sp["count"], "total_bbl": sp["total_bbl"], "corrected_excluded": sp["voided"]},
            "regulatory": [{**{k: r[k] for k in ("event_id", "date", "agency", "event_type", "protocol_id", "fine_usd", "status", "hours_to_report", "detail", "related_inspection_id")}, "ref": f"reg_event:{r['event_id']}"} for r in reversed(reg["rows"])],
            "fines": {"total_usd": reg["total_fines_usd"], "open_usd": reg["open_fines_usd"]},
            "rules": [{k: v for k, v in r.items() if k != "_evidence"} | {"ref": r["_evidence"]["ref"]} for r in d.list_rules(a["well_type"])],
            "notes": [{**n, "author": d.user_name(n["author_user_id"]), "ref": f"note:{n['note_id']}"} for n in reversed(notes["notes"])],
            "claims": [{**c, "author": d.user_name(c["author_user_id"]), "ref": f"claim:{c['claim_id']}"} for c in reversed(notes["claims"])],
            "corrections": corrections, "investigations": invest, "as_of": d.as_of.isoformat(),
        }

    def _corrections_for(self, asset_id: str, well_type: str, insp_ids: list[str]) -> list[dict]:
        d, st = self.d, self.state
        rows = []
        for c in st.corrections(active_only=False):
            mine = (c["target_type"] == "inspection" and c["target_id"] in set(insp_ids)) or (c["target_type"] == "asset" and c["target_id"] == asset_id) \
                or (c["target_type"] == "well_type" and c["target_id"] == well_type)
            if mine:
                rows.append({**{k: c[k] for k in ("correction_id", "kind", "target_type", "target_id", "field", "original_value", "new_value", "scope_kind", "scope_value", "reason", "created_at", "status", "conflicts")},
                             "author": d.user_name(c["author_user_id"]), "author_id": c["author_user_id"], "ref": f"correction:{c['correction_id']}"})
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows

    def corrections(self, asset_id: str) -> list[dict]:
        a = self.d.asset(asset_id)
        if not a:
            raise ViewError(404, "asset not found")
        ids = [r[0] for r in self.d.src.execute("SELECT inspection_id FROM inspections WHERE asset_id=?", (a["asset_id"],))]
        return self._corrections_for(a["asset_id"], a["well_type"], ids)

    # ------------------------------------------------------------------ shared knowledge overview
    def knowledge(self, viewer: str | None) -> dict:
        d = self.d
        corrections = []
        for c in reversed(self.state.corrections(active_only=False)[-200:]):
            corrections.append({**{k: c[k] for k in ("correction_id", "kind", "target_type", "target_id", "field", "original_value", "new_value", "scope_kind", "scope_value", "reason", "created_at", "status")},
                                "author": d.user_name(c["author_user_id"]), "author_id": c["author_user_id"], "ref": f"correction:{c['correction_id']}",
                                "orphaned": c["target_type"] == "inspection" and d.src.execute("SELECT 1 FROM inspections WHERE inspection_id=?", (c["target_id"],)).fetchone() is None})
        rows = self.state.c.execute("SELECT * FROM claims WHERE visibility='shared' OR author_user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 200", (viewer or "",)).fetchall()
        claims = [{**dict(r), "author": d.user_name(r["author_user_id"]), "ref": f"claim:{r['claim_id']}"} for r in rows]
        return {"corrections": corrections, "claims": claims, "audit": self.audit(object_type=None, object_id=None, viewer=viewer, limit=100)}

    # ------------------------------------------------------------------ audit
    def audit(self, *, object_type: str | None, object_id: str | None, viewer: str | None, limit: int = 50) -> list[dict]:
        rows = self.state.audit_trail(object_type, object_id, limit=max(1, min(limit, 200)))
        out = []
        private_claims = {c["claim_id"]: c["author_user_id"] for c in self.state.c.execute("SELECT claim_id, author_user_id FROM claims WHERE visibility='private'")}
        for r in rows:
            if r["object_type"] == "claim" and r["object_id"] in private_claims and private_claims[r["object_id"]] != viewer:
                continue                                    # another user's private claim: its existence is not revealed
            if r["object_type"] in ("conversation", "proposal") and r["actor_user_id"] != viewer:
                continue                                    # conversation/proposal audit entries are private to their actor
            out.append({"at": r["at"], "actor_id": r["actor_user_id"], "actor": self.d.user_name(r["actor_user_id"]), "action": r["action"], "object_type": r["object_type"],
                        "object_id": r["object_id"], "detail": json.loads(r["detail_json"] or "{}")})
        return out

    # ------------------------------------------------------------------ evidence
    def evidence(self, ref: str, user_id: str | None) -> dict:
        m = re.match(r"^([a-z_]+):(.{1,200})$", ref or "")
        if not m:
            raise ViewError(422, "malformed evidence reference")
        kind, ident = m.group(1), m.group(2)
        fn = getattr(self, f"_ev_{kind}", None)
        if fn is None:
            raise ViewError(404, f"'{kind}' evidence cannot be opened as a record" + (" (it is a calculated value; see the answer's trace)" if kind == "computed" else ""))
        out = fn(ident, user_id)
        out["ref"] = ref
        return out

    def _fields(self, pairs: list[tuple[str, Any]]) -> list[dict]:
        return [{"label": k, "value": v} for k, v in pairs if v not in (None, "")]

    def _ev_inspection(self, ident: str, user_id) -> dict:
        row = self.d.src.execute("SELECT * FROM inspections WHERE inspection_id=?", (ident,)).fetchone()
        if not row:
            raise ViewError(404, "inspection not found")
        e = self.fb.inspection(ident)
        d = self.d
        related = [{"ref": f"maintenance:{r[0]}", "title": f"Repair {r[0]}: {r[1]}"} for r in d.src.execute("SELECT maintenance_id, issue FROM maintenance WHERE triggered_by_inspection_id=?", (ident,))]
        related += [{"ref": f"reg_event:{r[0]}", "title": f"Regulator event {r[0]} ({r[1]})"} for r in d.src.execute("SELECT event_id, event_type FROM reg_events WHERE related_inspection_id=?", (ident,))]
        related += [{"ref": f"note:{r[0]}", "title": f"Note {r[0]}"} for r in d.src.execute("SELECT note_id FROM notes WHERE inspection_id=? OR text LIKE ?", (ident, f"%{ident}%"))]
        corr = [{"correction_id": c["correction_id"], "field": c["field"], "original": c["original"], "new": c["new"], "author": c["author_name"], "reason": c["reason"], "at": c["at"]} for c in e["applied_corrections"]]
        fields = self._fields([("Asset", row["asset_id"]), ("Inspected at (UTC)", row["inspected_at"]), ("Inspector", d.user_name(row["inspector_user_id"])), ("Method", row["method"]),
                               ("Wellhead temperature (recorded)", f"{row['wellhead_temp_c']} °C"), ("Ambient temperature (recorded)", f"{row['ambient_temp_c']} °C"),
                               ("Wellhead temperature (effective)", f"{e['wellhead_temp_c']} °C" if e["wellhead_temp_c"] != row["wellhead_temp_c"] else None),
                               ("Wellhead above air", f"{e['delta_c']} °C"), ("Minimum for production (rule)", f"{e['threshold_c']} °C" if e["threshold_c"] is not None else None),
                               ("Producing", {True: "yes", False: "no", None: "unknown"}[e["producing"]]), ("Spill detected (raw)", "yes" if row["spill_detected"] else None),
                               ("Spill volume (AI estimate in the data)", f"{row['spill_est_volume_bbl']} bbl" if row["spill_est_volume_bbl"] is not None else None),
                               ("Spill volume (effective)", f"{e['spill_est_volume_bbl']} bbl" if e["spill_est_volume_bbl"] != row["spill_est_volume_bbl"] else None),
                               ("Spill covers", f"{row['spill_area_pct']} % of pad" if row["spill_area_pct"] is not None else None),
                               ("Detection later corrected as not-oil", e["spill_voided"]["text"] if e["spill_voided"] else None),
                               ("Evidence images", ", ".join(json.loads(row["evidence_images"] or "[]")) + " (image files are not part of the dataset)" if json.loads(row["evidence_images"] or "[]") else None)])
        return {"type": "inspection", "id": ident, "category": "source", "label": "Source record", "title": f"Inspection {ident}", "timestamp": row["inspected_at"],
                "source": {"file": row["src_path"], "line": row["src_line"]}, "fields": fields, "excerpt": row["notes"] or None, "related": related, "corrections": corr,
                "actions": {"correct": {"asset_id": row["asset_id"], "inspection_id": ident}}}

    def _ev_maintenance(self, ident: str, user_id) -> dict:
        r = self.d.src.execute("SELECT * FROM maintenance WHERE maintenance_id=?", (ident,)).fetchone()
        if not r:
            raise ViewError(404, "repair not found")
        facts = [dict(x) for x in self.d.src.execute("SELECT fact_key, value_num, value_text, protocol_id, excerpt FROM report_facts WHERE maintenance_id=?", (ident,))]
        related = ([{"ref": f"report:{ident}", "title": f"Repair report for {ident}"}] if r["report_file"] else []) + \
                  ([{"ref": f"inspection:{r['triggered_by_inspection_id']}", "title": f"Triggering inspection {r['triggered_by_inspection_id']}"}] if r["triggered_by_inspection_id"] else [])
        return {"type": "maintenance", "id": ident, "category": "source", "label": "Source record", "title": f"Repair {ident}: {r['issue']}", "timestamp": r["started_at"],
                "source": {"file": r["src_path"], "line": r["src_line"]}, "excerpt": r["action_taken"], "related": related, "corrections": [], "extracted_facts": facts,
                "fields": self._fields([("Asset", r["asset_id"]), ("Type", r["maintenance_type"]), ("Equipment", r["equipment_kind"]), ("Started", r["started_at"]),
                                        ("Completed / planned end", r["completed_at"]), ("Technician", self.d.user_name(r["technician_user_id"])), ("Contractor", r["contractor"]),
                                        ("Repair duration", f"{r['downtime_hours']} h" if r["downtime_hours"] is not None else None), ("Cost (USD)", f"{r['cost_total']:,.0f} (labour {r['cost_labor']:,.0f}, parts {r['cost_parts']:,.0f})" if r["cost_total"] is not None else None)])}

    def _ev_reg_event(self, ident: str, user_id) -> dict:
        r = self.d.src.execute("SELECT * FROM reg_events WHERE event_id=?", (ident,)).fetchone()
        if not r:
            raise ViewError(404, "regulator event not found")
        related = [{"ref": f"inspection:{r['related_inspection_id']}", "title": f"Related inspection {r['related_inspection_id']}"}] if r["related_inspection_id"] else []
        return {"type": "reg_event", "id": ident, "category": "source", "label": "Source record", "title": f"{ident}: {r['event_type'].replace('_', ' ')}", "timestamp": r["date"],
                "source": {"file": r["src_path"], "line": r["src_line"]}, "excerpt": r["detail"], "related": related, "corrections": [],
                "fields": self._fields([("Asset", r["asset_id"]), ("Agency", r["agency"]), ("Protocol", r["protocol_id"]), ("Status", r["status"]), ("Fine (USD)", f"{r['fine_usd']:,.0f}" if r["fine_usd"] else None),
                                        ("Hours from discovery to report", r["hours_to_report"])])}

    def _ev_note(self, ident: str, user_id) -> dict:
        r = self.d.src.execute("SELECT * FROM notes WHERE note_id=?", (ident,)).fetchone()
        if not r:
            raise ViewError(404, "note not found")
        refs = sorted(set(INS_REF.findall(r["text"])) | ({r["inspection_id"]} if r["inspection_id"] else set()))
        return {"type": "note", "id": ident, "category": "note", "label": "Human note (dataset)", "title": f"Note {ident}", "timestamp": r["created_at"],
                "source": {"file": r["src_path"], "line": r["src_line"]}, "excerpt": r["text"], "corrections": [],
                "related": [{"ref": f"inspection:{i}", "title": f"Inspection {i}"} for i in refs],
                "fields": self._fields([("Asset", r["asset_id"]), ("Author", self.d.user_name(r["author_user_id"])), ("Type", r["note_type"].replace("_", " ")), ("Valid from", r["valid_from"]), ("Valid to", r["valid_to"] or "open-ended")])}

    def _ev_claim(self, ident: str, user_id) -> dict:
        r = self.state.c.execute("SELECT * FROM claims WHERE claim_id=?", (ident,)).fetchone()
        if not r or (r["visibility"] == "private" and r["author_user_id"] != user_id):
            raise ViewError(404, "claim not found")                         # a private claim is indistinguishable from a missing one
        return {"type": "claim", "id": ident, "category": "claim", "label": "User claim (unverified unless stated)", "title": f"Claim {ident}", "timestamp": r["created_at"], "source": None,
                "excerpt": r["text"], "corrections": [], "related": [{"ref": f"asset:{r['asset_id']}", "title": f"Asset {r['asset_id']}"}],
                "fields": self._fields([("Author", self.d.user_name(r["author_user_id"])), ("Type", r["claim_type"].replace("_", " ")), ("Visibility", r["visibility"]), ("Verification", r["verification"]),
                                        ("Checked against the data", r["verification_detail"]), ("Valid from", r["valid_from"]), ("Valid to", r["valid_to"])]),
                "audit": self.audit(object_type="claim", object_id=ident, viewer=user_id, limit=20)}

    def _ev_correction(self, ident: str, user_id) -> dict:
        c = self.state.get_correction(ident)
        if not c:
            raise ViewError(404, "correction not found")
        return {"type": "correction", "id": ident, "category": "correction", "label": "User correction (original kept)", "title": f"Correction {ident}", "timestamp": c["created_at"], "source": None,
                "excerpt": c["reason"], "related": ([{"ref": f"inspection:{c['target_id']}", "title": f"Corrected inspection {c['target_id']}"}] if c["target_type"] == "inspection" else []), "corrections": [],
                "fields": self._fields([("Author", self.d.user_name(c["author_user_id"])), ("Kind", c["kind"].replace("_", " ")), ("Target", f"{c['target_type']} {c['target_id']}"), ("Field", c["field"]),
                                        ("Original value", c["original_value"]), ("Corrected value", c["new_value"]), ("Scope", f"{c['scope_kind']}: {c['scope_value']}"), ("Status", c["status"]),
                                        ("Competing corrections", ", ".join(x["correction_id"] for x in c["conflicts"]) if c["conflicts"] else None)]),
                "audit": self.audit(object_type="correction", object_id=ident, viewer=user_id, limit=20),
                "actions": {"withdraw": {"correction_id": ident}} if c["status"] == "active" else {}}

    def _ev_asset(self, ident: str, user_id) -> dict:
        a = self.d.asset(ident)
        if not a:
            raise ViewError(404, "asset not found")
        return {"type": "asset", "id": a["asset_id"], "category": "source", "label": "Source record", "title": f"{a['asset_id']} · {a['name']}", "timestamp": a["installed_on"], "source": {"file": a["src_path"], "line": None},
                "excerpt": None, "related": [], "corrections": [],
                "fields": self._fields([("Well type", a["well_type"]), ("Area", a["area_id"]), ("Team", a["team_id"]), ("In charge", self.d.user_name(a["in_charge_user_id"])), ("Installed", a["installed_on"]),
                                        ("Recorded status (refreshed monthly)", a["recorded_status"]), ("Location", f"{a['lat']}, {a['lon']}" if a["lat"] is not None else "not recorded")])}

    def _doc(self, doc: Any) -> dict:
        text = doc["text"]
        return {"type": "document", "id": doc["doc_id"], "category": "document", "label": "Document", "title": doc["title"], "timestamp": None, "source": {"file": doc["path"], "line": None},
                "excerpt": text[:2500] + (" …" if len(text) > 2500 else ""), "related": [], "corrections": [],
                "fields": self._fields([("Kind", doc["kind"]), ("Asset", doc["asset_id"]), ("Repair", doc["maintenance_id"]), ("Well type", doc["well_type"]), ("Length", f"{len(text):,} characters")])}

    def _ev_document(self, ident: str, user_id) -> dict:
        doc = self.d.src.execute("SELECT * FROM documents WHERE doc_id=?", (ident,)).fetchone()
        if not doc:
            raise ViewError(404, "document not found")
        return self._doc(doc)

    def _ev_report(self, ident: str, user_id) -> dict:
        doc = self.d.src.execute("SELECT * FROM documents WHERE maintenance_id=? AND kind='report'", (ident,)).fetchone()
        if not doc:
            raise ViewError(404, "report not found")
        out = self._doc(doc)
        out["related"] = [{"ref": f"maintenance:{ident}", "title": f"Repair record {ident}"}]
        out["extracted_facts"] = [dict(x) for x in self.d.src.execute("SELECT fact_key, value_num, value_text, protocol_id, excerpt FROM report_facts WHERE maintenance_id=?", (ident,))]
        return out

    def _ev_rule(self, ident: str, user_id) -> dict:
        key, _, doc = ident.partition("@")
        if doc.startswith("user rule "):
            return self._ev_correction(doc[len("user rule "):], user_id) | {"label": "User-defined rule (not from the manual)", "category": "correction"}
        rows = [dict(r) for r in self.d.src.execute("SELECT * FROM rules WHERE rule_key=? AND source_doc=?", (key, doc))]
        if not rows:
            raise ViewError(404, "rule not found")
        r = rows[0]
        docrow = self.d.src.execute("SELECT text FROM documents WHERE doc_id=?", (doc,)).fetchone()
        passage = r["excerpt"]
        if docrow:
            probe = re.sub(r"\s+", " ", r["excerpt"])[:40]
            flat = re.sub(r"\s+", " ", docrow["text"])
            i = flat.find(probe) if probe else -1
            if i >= 0:
                passage = flat[max(0, i - 200): i + 500]
        return {"type": "rule", "id": ident, "category": "rule", "label": "Rule extracted from a manual/SOP", "title": f"{key} = {r['value_num']:g} {r['unit'] or ''}".strip(), "timestamp": None,
                "source": {"file": doc, "line": None}, "excerpt": passage, "related": [{"ref": f"document:{doc}", "title": f"Full document {doc}"}], "corrections": [],
                "fields": self._fields([("Rule", key), ("Value", f"{r['value_num']:g} {r['unit'] or ''}".strip()), ("Applies to", f"{r['scope_kind']} {r['scope_value']}"), ("Section", r["source_section"])])}

    def _ev_memory(self, ident: str, user_id) -> dict:
        r = self.state.c.execute("SELECT * FROM user_memory WHERE id=? AND user_id=?", (ident, user_id or "")).fetchone()
        if not r:
            raise ViewError(404, "memory item not found")                 # other users' memory is indistinguishable from missing
        return {"type": "memory", "id": ident, "category": "note", "label": "Your own earlier question (private)", "title": f"Earlier question: {r['question'][:70]}", "timestamp": r["created_at"], "source": None,
                "excerpt": r["question"], "related": [{"ref": f"asset:{w}", "title": w} for w in json.loads(r["entity_ids_json"] or "[]")[:12]], "corrections": [],
                "fields": self._fields([("Result set from tool", r["tool_name"]), ("Wells", ", ".join(json.loads(r["entity_ids_json"] or "[]")))])}

    # ------------------------------------------------------------------ example questions drawn from the loaded data
    def examples(self) -> list[dict]:
        d = self.d
        areas = list(d.areas().values())
        if not areas:
            return []
        a0 = areas[0]["name"]
        a1 = areas[-1]["name"]
        a2 = areas[min(1, len(areas) - 1)]["name"]
        f = d.fleet_status(only="not_producing")
        well = next((r["asset_id"] for r in f["rows"] if r.get("reasons")), f["rows"][0]["asset_id"] if f["rows"] else None)
        sp = d.spills(None, TimeWindow(last_n=90, unit="days"))
        spill_area = d.areas().get(sp["rows"][-1]["area"], {}).get("name") if sp["rows"] else a1
        yr = d.as_of.year
        out = []
        if well:
            out += [{"question": f"Is {well} producing oil right now? When was it last inspected, and by whom? Show me its status card.", "topic": "One well, current status"},
                    {"question": f"Show a line chart of {well}'s wellhead temperature against air temperature for the last 13 weeks, and mark when it was not producing oil.", "topic": "Trend over time"},
                    {"question": f"Which rules has {well} broken in the last 12 months, and what is still open?", "topic": "Compliance across sources"}]
        out += [{"question": f"Which wells in {a1} are not producing oil right now, and why? Show them in a table.", "topic": "List with reasons"},
                {"question": f"Which 3 wells in {a0} had the most downtime in the last 4 months? Show a bar chart.", "topic": "Comparison"},
                {"question": f"Show on a map every well in {spill_area} with a real spill in the last 90 days.", "topic": "Where"},
                {"question": f"Show a heatmap of {yr} spills by geographic area and month.", "topic": "Two dimensions"},
                {"question": f"In {a2}, which spills in the last 90 days were not contained within the SOP time limit?", "topic": "Report-derived facts"},
                {"question": f"Which wells in {a0} need a root-cause analysis on their pump jack?", "topic": "Rules from the manual"}]
        return out
