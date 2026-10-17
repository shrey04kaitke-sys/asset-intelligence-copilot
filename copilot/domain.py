"""Deterministic domain logic. Nothing here calls a model.

All numbers, deadlines, classifications and thresholds are computed from ingested records, rules
extracted from the manuals/SOPs, and user-supplied overlays (corrections, claims). Each result
carries evidence references so callers can cite exactly what was used.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .state import StateStore
from .timewin import TimeWindow, day_bounds

INSPECTION_FIELDS = {"wellhead_temp_c", "ambient_temp_c", "spill_est_volume_bbl", "spill_area_pct", "spill_detected"}
STATUS_NOTE_TYPES = ("operator_note", "operator_feedback")
INS_REF = re.compile(r"INS-\d+-\d+")
STOP_HINT = re.compile(r"shut[ -]?in|shutdown|workover|waiting on|restart|outage|planned|tie-in|offline|down for", re.I)


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def r1(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x + 0.0, nd)


def ev(etype: str, eid: str, title: str, fields: dict | None = None, path: str | None = None, kind: str = "record") -> dict:
    """Evidence reference. `kind`: record (observed), computed, note, document, correction, claim."""
    return {"ref": f"{etype}:{eid}", "type": etype, "id": eid, "title": title, "fields": fields or {}, "path": path, "kind": kind}


@dataclass
class Rule:
    key: str
    value: float | None
    unit: str | None
    source_doc: str
    section: str
    excerpt: str
    origin: str = "document"  # document | user_override
    override_id: str | None = None
    author: str | None = None

    def evidence(self) -> dict:
        t = f"{self.key} = {self.value:g} {self.unit or ''}".strip() if self.value is not None else self.key
        fields = {"value": self.value, "unit": self.unit, "section": self.section, "origin": self.origin}
        if self.override_id:
            fields.update({"override": self.override_id, "author": self.author})
        return ev("rule", f"{self.key}@{self.source_doc}", t, {**fields, "excerpt": self.excerpt[:240]}, self.source_doc, "document")


class Domain:
    def __init__(self, src: sqlite3.Connection, state: StateStore, today: date | None = None):
        self.src, self.state = src, state
        meta = {r["key"]: r["value"] for r in src.execute("SELECT key, value FROM meta")}
        self.as_of: date = today or date.fromisoformat(meta.get("as_of") or date.today().isoformat())
        self.as_of_basis = meta.get("as_of_basis", "unknown")
        self.now = datetime(self.as_of.year, self.as_of.month, self.as_of.day, tzinfo=timezone.utc)
        self.last_ingest_at = meta.get("last_ingest_at")
        self._users: dict[str, dict] | None = None
        self._areas: dict[str, dict] | None = None

    # ------------------------------------------------------------------ directory data
    def users(self) -> dict[str, dict]:
        if self._users is None:
            self._users = {r["user_id"]: dict(r) for r in self.src.execute("SELECT * FROM users")}
        return self._users

    def user_name(self, uid: str | None) -> str:
        u = self.users().get(uid or "")
        return u["name"] if u else (uid or "unknown")

    def areas(self) -> dict[str, dict]:
        if self._areas is None:
            self._areas = {r["area_id"]: dict(r) for r in self.src.execute("SELECT * FROM areas")}
        return self._areas

    def resolve_area(self, text: str | None) -> dict | None:
        if not text:
            return None
        t = re.sub(r"\s+", " ", text.strip().lower())
        best = None
        for a in self.areas().values():
            ids = {a["area_id"].lower(), (a["name"] or "").lower()}
            if t in ids:
                return a
            if any(i and (i in t or t in i) for i in ids):
                best = best or a
        return best

    def well_types(self) -> list[str]:
        return [r[0] for r in self.src.execute("SELECT DISTINCT well_type FROM assets ORDER BY 1")]

    def assets(self, *, area: str | None = None, well_type: str | None = None, team: str | None = None,
               asset_ids: list[str] | None = None) -> list[dict]:
        q, args = "SELECT * FROM assets WHERE 1=1", []
        if area:
            a = self.resolve_area(area)
            if not a:
                return []
            q += " AND area_id=?"; args.append(a["area_id"])
        if well_type:
            q += " AND well_type=?"; args.append(well_type)
        if team:
            q += " AND team_id=?"; args.append(team)
        if asset_ids:
            q += f" AND asset_id IN ({','.join('?' * len(asset_ids))})"; args += list(asset_ids)
        q += " ORDER BY asset_id"
        return [dict(r) for r in self.src.execute(q, args)]

    def asset(self, asset_id: str) -> dict | None:
        r = self.src.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id.upper(),)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ rules
    def rule(self, key: str, well_type: str | None = None, asset_id: str | None = None) -> Rule | None:
        # 1. user overrides: asset scope beats well-type scope; newest wins
        ov = []
        if asset_id:
            ov += self.state.corrections(kind="rule_override", field=key, target_type="asset", target_id=asset_id)
        if well_type:
            ov += self.state.corrections(kind="rule_override", field=key, target_type="well_type", target_id=well_type)
        ov.sort(key=lambda c: (c["target_type"] == "asset", c["created_at"]))
        if ov:
            c = ov[-1]
            base = self._doc_rule(key, well_type)
            return Rule(key, float(c["new_value"]), base.unit if base else None, f"user rule {c['correction_id']}",
                        f"{c['scope_kind']}:{c['scope_value']}", c.get("reason") or "", "user_override", c["correction_id"], c["author_user_id"])
        return self._doc_rule(key, well_type)

    def _doc_rule(self, key: str, well_type: str | None) -> Rule | None:
        seen, wt = set(), well_type
        while wt and wt not in seen:
            seen.add(wt)
            r = self.src.execute("SELECT * FROM rules WHERE rule_key=? AND scope_kind='well_type' AND scope_value=?", (key, wt)).fetchone()
            if r:
                return Rule(key, r["value_num"], r["unit"], r["source_doc"], r["source_section"], r["excerpt"])
            p = self.src.execute("SELECT excerpt FROM rules WHERE rule_key='inherits_from' AND scope_value=?", (wt,)).fetchone()
            wt = p["excerpt"] if p else None
        r = self.src.execute("SELECT * FROM rules WHERE rule_key=? AND scope_kind='global'", (key,)).fetchone()
        return Rule(key, r["value_num"], r["unit"], r["source_doc"], r["source_section"], r["excerpt"]) if r else None

    # ------------------------------------------------------------------ effective inspections
    def _correction_maps(self):
        void_notes: dict[str, dict] = {}
        for r in self.src.execute("SELECT * FROM notes WHERE note_type='correction' AND inspection_id IS NOT NULL ORDER BY created_at"):
            void_notes[r["inspection_id"]] = dict(r)
        user: dict[str, list[dict]] = defaultdict(list)
        for c in self.state.corrections(target_type="inspection"):
            user[c["target_id"]].append(c)
        return void_notes, user

    def eff_inspections(self, asset_ids: list[str] | None, start: date | None = None, end: date | None = None,
                        well_type_by_asset: dict[str, str] | None = None, newest_first: bool = False,
                        limit: int | None = None) -> list[dict]:
        q, args = "SELECT i.*, a.well_type, a.area_id FROM inspections i JOIN assets a USING(asset_id) WHERE 1=1", []
        if asset_ids is not None:
            if not asset_ids:
                return []
            q += f" AND i.asset_id IN ({','.join('?' * len(asset_ids))})"; args += list(asset_ids)
        if start:
            q += " AND i.inspected_at >= ?"; args.append(day_bounds(start))
        if end:
            q += " AND i.inspected_at < ?"; args.append(day_bounds(end, end=True))
        q += " ORDER BY i.asset_id, i.inspected_at" + (" DESC" if newest_first else "")
        if limit:
            q += f" LIMIT {int(limit)}"
        void_notes, user = self._correction_maps()
        rule_cache: dict[tuple, Rule | None] = {}
        out = []
        for r in self.src.execute(q, args):
            out.append(self._effective(dict(r), void_notes, user, rule_cache))
        return out

    def _effective(self, row: dict, void_notes: dict, user: dict, rule_cache: dict) -> dict:
        iid = row["inspection_id"]
        e = {k: row[k] for k in ("inspection_id", "asset_id", "inspected_at", "inspector_user_id", "method", "notes", "well_type", "area_id")}
        e["date"] = row["inspected_at"][:10]
        raw = {f: row[f] for f in INSPECTION_FIELDS}
        raw["spill_detected"] = bool(raw["spill_detected"])
        vals = dict(raw)
        applied: list[dict] = []
        conflicts: list[dict] = []
        by_field: dict[str, list[dict]] = defaultdict(list)
        for c in user.get(iid, []):
            if c["field"] in INSPECTION_FIELDS:
                by_field[c["field"]].append(c)
        for f, cs in by_field.items():
            cs.sort(key=lambda c: c["created_at"])
            win = cs[-1]
            vals[f] = bool(win["new_value"]) if f == "spill_detected" else (None if win["new_value"] is None else float(win["new_value"]))
            applied.append({"correction_id": win["correction_id"], "field": f, "original": raw[f], "new": vals[f],
                            "author": win["author_user_id"], "author_name": self.user_name(win["author_user_id"]),
                            "reason": win["reason"], "at": win["created_at"], "kind": win["kind"]})
            others = [c for c in cs[:-1] if c["author_user_id"] != win["author_user_id"] and c["new_value"] != win["new_value"]]
            if others:
                conflicts.append({"field": f, "winning": win["correction_id"], "competing": [c["correction_id"] for c in others]})
        voided = None
        if iid in void_notes:
            n = void_notes[iid]
            voided = {"source": "dataset_note", "note_id": n["note_id"], "author": n["author_user_id"],
                      "author_name": self.user_name(n["author_user_id"]), "text": n["text"], "at": n["created_at"]}
        elif raw["spill_detected"] and vals["spill_detected"] is False:
            a = next(x for x in applied if x["field"] == "spill_detected")
            voided = {"source": "user_correction", "note_id": a["correction_id"], "author": a["author"],
                      "author_name": a["author_name"], "text": a["reason"] or "", "at": a["at"]}
        e.update(raw_values=raw, ambient_temp_c=vals["ambient_temp_c"], wellhead_temp_c=vals["wellhead_temp_c"],
                 spill_detected_raw=raw["spill_detected"], spill_est_volume_bbl=vals["spill_est_volume_bbl"],
                 spill_area_pct=vals["spill_area_pct"], applied_corrections=applied, correction_conflicts=conflicts,
                 spill_voided=voided)
        e["is_spill"] = bool(vals["spill_detected"]) and voided is None
        wt = row["well_type"]
        ck = (wt, "production.min_delta_c", row["asset_id"])
        if ck not in rule_cache:
            rule_cache[ck] = self.rule("production.min_delta_c", wt, row["asset_id"])
        thr = rule_cache[ck]
        amb, wh = vals["ambient_temp_c"], vals["wellhead_temp_c"]
        delta = round(wh - amb, 3) if amb is not None and wh is not None else None
        e["delta_c"] = r1(delta)
        e["threshold_c"] = thr.value if thr else None
        e["threshold_origin"] = thr.origin if thr else None
        e["producing"] = None if (delta is None or thr is None or thr.value is None) else delta >= thr.value
        return e

    # ------------------------------------------------------------------ status of one asset
    def status(self, asset_id: str, viewer: str | None = None) -> dict:
        a = self.asset(asset_id)
        if not a:
            return {"error": f"unknown asset {asset_id}"}
        rows = self.eff_inspections([a["asset_id"]], newest_first=True, limit=1)
        evidence = [ev("asset", a["asset_id"], f"{a['name']} ({a['well_type']})", {
            "recorded_status": a["recorded_status"], "area": a["area_id"], "location": [a["lat"], a["lon"]]},
            a["src_path"])]
        if not rows:
            return {"asset": a, "latest": None, "producing": None, "evidence": evidence,
                    "warnings": ["no inspections on record for this asset"], "explanations": [], "freshness": self.freshness(None)}
        li = rows[0]
        evidence.append(self.inspection_evidence(li))
        thr = self.rule("production.min_delta_c", a["well_type"], a["asset_id"])
        if thr:
            evidence.append(thr.evidence())
        expl = self.explain_nonproducing(a, li, viewer) if li["producing"] is False else {"items": [], "observation": None}
        for it in expl["items"]:
            evidence.append(it["evidence"])
        warnings = []
        stale = None
        rs = a["recorded_status"]
        if li["producing"] is False and rs == "active":
            stale = "recorded_status says 'active' but the latest inspection shows the well not producing oil"
        elif li["producing"] is True and rs == "down":
            stale = "recorded_status says 'down' but the latest inspection shows the well producing oil"
        if stale:
            warnings.append(stale + " (recorded status is refreshed monthly; inspections are the truth)")
        if li["applied_corrections"]:
            warnings.append("latest inspection includes applied corrections (original values preserved)")
        if li["correction_conflicts"]:
            warnings.append("competing corrections exist for this inspection; the most recent was used")
        obs_conflict = None
        if li["producing"] is True and li["notes"]:
            if re.search(r"not moving|stopped|not running|tripped", li["notes"], re.I):
                obs_conflict = f"Inspector note \"{li['notes']}\" disagrees with a producing reading"
                warnings.append(obs_conflict)
        if li["producing"] is False and not expl["items"]:
            warnings.append("no recorded reason for the stop; suggest a field check")
        fr = self.freshness(li)
        return {"asset": a, "latest": li, "producing": li["producing"], "delta_c": li["delta_c"], "threshold": thr.value if thr else None,
                "recorded_status": rs, "stale_recorded_status": stale, "explanations": expl["items"],
                "inspector_observation": expl["observation"], "observation_conflict": obs_conflict,
                "no_recorded_reason": li["producing"] is False and not expl["items"],
                "evidence": evidence, "warnings": warnings, "freshness": fr}

    def freshness(self, latest: dict | None) -> dict:
        if not latest:
            return {"as_of": self.as_of.isoformat(), "basis": self.as_of_basis, "latest_inspection": None, "stale": True}
        age = (self.as_of - date.fromisoformat(latest["date"])).days
        gap = self.rule("rc01.max_inspection_gap_days")
        return {"as_of": self.as_of.isoformat(), "basis": self.as_of_basis, "latest_inspection": latest["inspected_at"],
                "age_days": age, "stale": bool(gap and gap.value and age > gap.value), "last_ingest_at": self.last_ingest_at}

    def inspection_evidence(self, i: dict) -> dict:
        f = {"at": i["inspected_at"], "wellhead_c": i["wellhead_temp_c"], "ambient_c": i["ambient_temp_c"], "delta_c": i["delta_c"],
             "inspector": self.user_name(i["inspector_user_id"]), "method": i["method"]}
        if i["notes"]:
            f["notes"] = i["notes"]
        if i["spill_detected_raw"]:
            f.update(spill_bbl=i["spill_est_volume_bbl"], spill_voided=bool(i["spill_voided"]))
        if i["applied_corrections"]:
            f["corrections"] = [c["correction_id"] for c in i["applied_corrections"]]
        return ev("inspection", i["inspection_id"], f"Inspection {i['inspection_id']} ({i['date']})", f,
                  f"assets/{i['asset_id']}/inspections.jsonl")

    def explain_nonproducing(self, a: dict, insp: dict, viewer: str | None) -> dict:
        t = insp["inspected_at"]
        d = insp["date"]
        items: list[dict] = []
        for m in self.src.execute(
                "SELECT * FROM maintenance WHERE asset_id=? AND started_at<=? AND (completed_at IS NULL OR completed_at>=?) ORDER BY started_at",
                (a["asset_id"], t, t)):
            open_now = m["completed_at"] is None or m["completed_at"] > self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
            items.append({"kind": "repair", "id": m["maintenance_id"],
                          "text": f"{m['maintenance_type']} repair {m['maintenance_id']} on {m['equipment_kind']}: {m['issue']} "
                                  f"(started {m['started_at'][:10]}, {'still open' if open_now else 'completed ' + m['completed_at'][:10]})",
                          "evidence": ev("maintenance", m["maintenance_id"], f"{m['maintenance_id']} {m['issue']}", {
                              "equipment": m["equipment_kind"], "type": m["maintenance_type"], "started": m["started_at"],
                              "completed_or_planned": m["completed_at"], "issue": m["issue"]}, f"assets/{a['asset_id']}/maintenance.jsonl")})
        for n in self.src.execute(
                "SELECT * FROM notes WHERE asset_id=? AND note_type IN (?,?) AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR valid_to>=?) ORDER BY created_at", (a["asset_id"], *STATUS_NOTE_TYPES, d, d)):
            if INS_REF.search(n["text"]):
                continue  # classification disputes about a specific inspection, not a status reason
            if not STOP_HINT.search(n["text"]):
                continue  # context note (e.g. calibration remark) that does not explain a stop
            items.append({"kind": "note", "id": n["note_id"],
                          "text": f"note {n['note_id']} by {self.user_name(n['author_user_id'])}: {n['text']}",
                          "evidence": ev("note", n["note_id"], f"Note {n['note_id']}", {
                              "author": self.user_name(n["author_user_id"]), "text": n["text"], "valid_from": n["valid_from"], "valid_to": n["valid_to"]},
                              f"assets/{a['asset_id']}/context.jsonl", "note")})
        for c in self.state.claims(a["asset_id"], viewer):
            if c["claim_type"] in ("planned_shutdown", "status_not_producing") and self._claim_covers(c, d):
                items.append({"kind": "claim", "id": c["claim_id"], "unverified": c["verification"] == "unverified",
                              "text": f"claim {c['claim_id']} by {self.user_name(c['author_user_id'])} ({c['verification']}): {c['text']}",
                              "evidence": ev("claim", c["claim_id"], f"Claim {c['claim_id']}", {
                                  "author": self.user_name(c["author_user_id"]), "text": c["text"], "verification": c["verification"],
                                  "visibility": c["visibility"], "valid_from": c["valid_from"], "valid_to": c["valid_to"]}, None, "claim")})
        obs = insp["notes"] or None
        return {"items": items, "observation": obs}

    @staticmethod
    def _claim_covers(c: dict, d: str) -> bool:
        return (not c["valid_from"] or c["valid_from"][:10] <= d) and (not c["valid_to"] or c["valid_to"][:10] >= d)

    # ------------------------------------------------------------------ fleet snapshot
    def fleet_status(self, *, area: str | None = None, well_type: str | None = None, only: str | None = None,
                     viewer: str | None = None, asset_ids: list[str] | None = None) -> dict:
        """Latest-inspection status for many wells. only: 'not_producing' | 'producing' | None."""
        assets = self.assets(area=area, well_type=well_type, asset_ids=asset_ids)
        ids = [a["asset_id"] for a in assets]
        latest = {i["asset_id"]: i for i in self._latest_per_asset(ids)}
        rows, evidence = [], []
        for a in assets:
            li = latest.get(a["asset_id"])
            if not li:
                continue
            if only == "not_producing" and li["producing"] is not False:
                continue
            if only == "producing" and li["producing"] is not True:
                continue
            row = {"asset_id": a["asset_id"], "area": a["area_id"], "well_type": a["well_type"], "inspection_id": li["inspection_id"],
                   "inspected_at": li["inspected_at"], "delta_c": li["delta_c"], "threshold_c": li["threshold_c"],
                   "producing": li["producing"], "recorded_status": a["recorded_status"]}
            if li["producing"] is False:
                ex = self.explain_nonproducing(a, li, viewer)
                row["reasons"] = [x["text"] for x in ex["items"]]
                row["reason_refs"] = [x["evidence"]["ref"] for x in ex["items"]]
                row["inspector_observation"] = ex["observation"]
                row["no_recorded_reason"] = not ex["items"]
                evidence += [x["evidence"] for x in ex["items"]]
            row["stale_recorded_status"] = ((li["producing"] is False and a["recorded_status"] == "active") or
                                            (li["producing"] is True and a["recorded_status"] == "down"))
            row["corrections"] = [c["correction_id"] for c in li["applied_corrections"]]
            rows.append(row)
            evidence.append(self.inspection_evidence(li))
        return {"rows": rows, "evidence": _dedupe(evidence), "scope_assets": len(assets)}

    def _latest_per_asset(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.src.execute(
            f"SELECT inspection_id FROM inspections i WHERE asset_id IN ({qs}) AND inspected_at=("
            f"SELECT MAX(inspected_at) FROM inspections WHERE asset_id=i.asset_id)", ids).fetchall()
        iids = [r[0] for r in rows]
        if not iids:
            return []
        void_notes, user = self._correction_maps()
        cache: dict = {}
        out = []
        for chunk in _chunks(iids, 500):
            q = ("SELECT i.*, a.well_type, a.area_id FROM inspections i JOIN assets a USING(asset_id) "
                 f"WHERE inspection_id IN ({','.join('?' * len(chunk))})")
            out += [self._effective(dict(r), void_notes, user, cache) for r in self.src.execute(q, chunk)]
        return out

    # ------------------------------------------------------------------ downtime
    def downtime(self, asset_ids: list[str], window: TimeWindow, viewer: str | None = None) -> dict:
        start, end, label = window.resolve(self.as_of)
        ins = self.eff_inspections(asset_ids, start, end)
        per: dict[str, dict] = {}
        for i in ins:
            p = per.setdefault(i["asset_id"], {"asset_id": i["asset_id"], "well_type": i["well_type"], "area": i["area_id"],
                                               "nonproducing": [], "inspections": 0, "unknown": 0, "corrected": 0})
            p["inspections"] += 1
            if i["producing"] is False:
                p["nonproducing"].append(i)
            elif i["producing"] is None:
                p["unknown"] += 1
            if i["applied_corrections"]:
                p["corrected"] += 1
        rows = []
        for p in per.values():
            rows.append({"asset_id": p["asset_id"], "area": p["area"], "well_type": p["well_type"],
                         "downtime_days": 7 * len(p["nonproducing"]), "nonproducing_inspections": len(p["nonproducing"]),
                         "inspections_in_window": p["inspections"], "unknown_readings": p["unknown"],
                         "inspection_ids": [i["inspection_id"] for i in p["nonproducing"]],
                         "dates": [i["date"] for i in p["nonproducing"]], "corrected_inspections": p["corrected"]})
        rows.sort(key=lambda r: (-r["downtime_days"], r["asset_id"]))
        ev_list = []
        for p in per.values():
            ev_list += [self.inspection_evidence(i) for i in p["nonproducing"]][:60]
        return {"rows": rows, "window": {"start": start.isoformat() if start else None, "end": end.isoformat(), "label": label},
                "method": "7 days per weekly inspection in the window that shows the well not producing oil; missed inspections add none",
                "evidence": _dedupe(ev_list)}

    def producing_series(self, asset_id: str, window: TimeWindow) -> dict:
        start, end, label = window.resolve(self.as_of)
        ins = self.eff_inspections([asset_id], start, end)
        pts = [{"inspection_id": i["inspection_id"], "date": i["date"], "wellhead_temp_c": i["wellhead_temp_c"], "ambient_temp_c": i["ambient_temp_c"],
                "delta_c": i["delta_c"], "threshold_c": i["threshold_c"], "producing": i["producing"],
                "corrected": bool(i["applied_corrections"])} for i in ins]
        return {"points": pts, "window": {"start": start.isoformat() if start else None, "end": end.isoformat(), "label": label},
                "evidence": [self.inspection_evidence(i) for i in ins]}

    # ------------------------------------------------------------------ spills
    def spill_class(self, volume: float | None, well_type: str | None = None) -> str:
        if volume is None:
            return "unknown"
        mj = self.rule("spill.class.major_min_bbl")
        rp = self.rule("spill.class.reportable_min_bbl")
        md = self.rule("spill.class.moderate_min_bbl")
        if mj and mj.value is not None and volume >= mj.value:
            return "major"
        if rp and rp.value is not None and volume >= rp.value:
            return "reportable"
        if md and md.value is not None and volume >= md.value:
            return "moderate"
        return "minor" if (md and md.value is not None) else "unclassified"

    def spills(self, asset_ids: list[str] | None, window: TimeWindow, include_voided: bool = False) -> dict:
        start, end, label = window.resolve(self.as_of)
        ins = [i for i in self.eff_inspections(asset_ids, start, end) if i["spill_detected_raw"] or i["is_spill"]]
        rows, evidence = [], []
        mtr = defaultdict(list)
        for m in self.src.execute("SELECT maintenance_id, asset_id, triggered_by_inspection_id, report_file, issue FROM maintenance "
                                  "WHERE triggered_by_inspection_id IS NOT NULL"):
            mtr[m["triggered_by_inspection_id"]].append(dict(m))
        regs = defaultdict(list)
        for r in self.src.execute("SELECT event_id, related_inspection_id FROM reg_events WHERE related_inspection_id IS NOT NULL"):
            regs[r["related_inspection_id"]].append(r["event_id"])
        talk = defaultdict(list)  # informal notes that discuss a specific inspection (not formal corrections)
        for n in self.src.execute("SELECT * FROM notes WHERE note_type!='correction' AND text LIKE '%INS-%' ORDER BY created_at"):
            for ref in set(INS_REF.findall(n["text"])):
                talk[ref].append({"note_id": n["note_id"], "author": n["author_user_id"], "author_name": self.user_name(n["author_user_id"]),
                                  "role": self.users().get(n["author_user_id"], {}).get("role"), "text": n["text"], "at": n["created_at"]})
        for i in ins:
            if not i["is_spill"] and not include_voided:
                continue
            raw_vol = i["raw_values"]["spill_est_volume_bbl"]
            vol = i["spill_est_volume_bbl"]
            a = self.asset(i["asset_id"])
            row = {"inspection_id": i["inspection_id"], "asset_id": i["asset_id"], "area": i["area_id"], "date": i["date"],
                   "inspected_at": i["inspected_at"], "volume_bbl": vol, "original_volume_bbl": raw_vol if vol != raw_vol else None,
                   "area_pct": i["spill_area_pct"], "class": self.spill_class(vol, i["well_type"]),
                   "voided": bool(i["spill_voided"]), "void_reason": (i["spill_voided"] or {}).get("text"),
                   "void_source": (i["spill_voided"] or {}).get("note_id"), "lat": a["lat"] if a else None, "lon": a["lon"] if a else None,
                   "inspector_note": i["notes"], "repairs": [m["maintenance_id"] for m in mtr.get(i["inspection_id"], [])],
                   "regulatory_events": regs.get(i["inspection_id"], []),
                   "corrections": [c["correction_id"] for c in i["applied_corrections"]],
                   "informal_notes": talk.get(i["inspection_id"], [])}
            rows.append(row)
            evidence.append(self.inspection_evidence(i))
            for n in row["informal_notes"]:
                evidence.append(ev("note", n["note_id"], f"Note {n['note_id']}", {"author": n["author_name"], "role": n["role"], "text": n["text"]},
                                   f"assets/{i['asset_id']}/context.jsonl", "note"))
            if i["spill_voided"]:
                v = i["spill_voided"]
                evidence.append(ev("note" if v["source"] == "dataset_note" else "correction", v["note_id"],
                                   f"Correction {v['note_id']}", {"author": v["author_name"], "text": v["text"]}, None, "correction"))
        rows.sort(key=lambda r: (r["date"], r["asset_id"]))
        total = round(sum(r["volume_bbl"] or 0 for r in rows if not r["voided"]), 3)
        counted = [r for r in rows if not r["voided"]]
        return {"rows": rows, "count": len(counted), "total_bbl": total,
                "detections": len(rows), "voided": len(rows) - len(counted),
                "window": {"start": start.isoformat() if start else None, "end": end.isoformat(), "label": label},
                "evidence": _dedupe(evidence)}

    # ------------------------------------------------------------------ compliance
    def spill_compliance(self, spill: dict) -> dict:
        """Evaluate SR-01..SR-03 for one effective spill row. Returns checks with verdicts and evidence."""
        a = self.asset(spill["asset_id"])
        wt = a["well_type"] if a else None
        disc = parse_dt(spill["inspected_at"])
        cls, vol = spill["class"], spill["volume_bbl"]
        checks, evidence, flags = [], [], []
        rep_row = self.src.execute(
            "SELECT m.maintenance_id, m.report_file FROM maintenance m WHERE triggered_by_inspection_id=? AND report_file IS NOT NULL ORDER BY started_at",
            (spill["inspection_id"],)).fetchall()
        # SR-01
        sr01 = self.rule("sr01.report_hours")
        sr01m = self.rule("sr01.major_report_hours")
        if cls in ("reportable", "major") and sr01:
            limit = sr01m.value if (cls == "major" and sr01m and sr01m.value) else sr01.value
            evs = self.src.execute("SELECT * FROM reg_events WHERE related_inspection_id=? AND event_type='spill_report_filed' ORDER BY date",
                                   (spill["inspection_id"],)).fetchall()
            due = disc + timedelta(hours=limit)
            if evs:
                e = evs[0]
                hrs = e["hours_to_report"]
                v = "met" if (hrs is not None and hrs <= limit) else ("breached" if hrs is not None else "unknown")
                checks.append({"protocol": "SR-01", "verdict": v, "limit_hours": limit, "actual_hours": hrs,
                               "detail": f"written notice filed {hrs} h after discovery (limit {limit:g} h)",
                               "refs": [f"reg_event:{e['event_id']}"]})
                evidence.append(ev("reg_event", e["event_id"], f"{e['event_id']} {e['event_type']}", {
                    "date": e["date"], "hours_to_report": hrs, "detail": e["detail"], "status": e["status"]}, f"assets/{spill['asset_id']}/regulatory-events.jsonl"))
                for n in self.src.execute("SELECT * FROM reg_events WHERE related_inspection_id=? AND event_type='notice_of_violation'", (spill["inspection_id"],)):
                    evidence.append(ev("reg_event", n["event_id"], f"{n['event_id']} notice_of_violation", {
                        "date": n["date"], "fine_usd": n["fine_usd"], "status": n["status"], "detail": n["detail"]}, f"assets/{spill['asset_id']}/regulatory-events.jsonl"))
            elif self.now > due:
                checks.append({"protocol": "SR-01", "verdict": "no_report_on_record_overdue", "limit_hours": limit, "actual_hours": None,
                               "detail": f"{cls} spill ({vol:g} bbl); written notice was due by {due.strftime('%Y-%m-%d %H:%M')} UTC and none is recorded",
                               "refs": [], "due": due.strftime("%Y-%m-%dT%H:%M:%SZ")})
                flags.append("possible SR-01 breach: no written notice on record past the deadline")
            else:
                checks.append({"protocol": "SR-01", "verdict": "pending", "limit_hours": limit, "actual_hours": None,
                               "detail": f"written notice due by {due.strftime('%Y-%m-%d %H:%M')} UTC", "refs": [], "due": due.strftime("%Y-%m-%dT%H:%M:%SZ")})
            evidence.append(sr01.evidence())
        # SR-02 (every spill): containment time lives in the repair report
        sr02 = self.rule("sr02.contain_hours")
        if sr02:
            fact = None
            for r in rep_row:
                f = self.src.execute("SELECT * FROM report_facts WHERE maintenance_id=? AND fact_key='containment_hours'", (r["maintenance_id"],)).fetchone()
                if f:
                    fact = (r, f)
                    break
            if fact:
                r, f = fact
                hrs = f["value_num"]
                checks.append({"protocol": "SR-02", "verdict": "met" if hrs <= sr02.value else "breached", "limit_hours": sr02.value,
                               "actual_hours": hrs, "detail": f"contained about {hrs:g} h after discovery (limit {sr02.value:g} h), per report {r['maintenance_id']}",
                               "refs": [f"report:{r['maintenance_id']}"]})
                evidence.append(ev("report", r["maintenance_id"], f"Repair report {r['maintenance_id']}", {
                    "excerpt": f["excerpt"]}, f"assets/{spill['asset_id']}/{r['report_file']}", "document"))
            else:
                checks.append({"protocol": "SR-02", "verdict": "unknown", "limit_hours": sr02.value, "actual_hours": None,
                               "detail": "no containment time found in any repair report", "refs": []})
            evidence.append(sr02.evidence())
        # SR-03
        vd = self.rule("sr03.verify_days")
        if cls in ("reportable", "major") and vd and vd.value:
            due_d = (disc + timedelta(days=vd.value)).date()
            site = self.src.execute(
                "SELECT * FROM reg_events WHERE related_inspection_id=? AND protocol_id='SR-03' ORDER BY date DESC", (spill["inspection_id"],)).fetchall()
            closed = [s for s in site if s["status"] == "closed"]
            refs = [f"reg_event:{s['event_id']}" for s in site]
            for s in site:
                evidence.append(ev("reg_event", s["event_id"], f"{s['event_id']} {s['event_type']}", {
                    "date": s["date"], "status": s["status"], "detail": s["detail"]}, f"assets/{spill['asset_id']}/regulatory-events.jsonl"))
            if closed:
                v, d = "met", f"agency verification closed ({closed[0]['event_id']})"
            elif self.as_of > due_d:
                v, d = "breached", f"agency verification due by {due_d.isoformat()} and still open/not recorded"
            else:
                v, d = "pending", f"agency verification due by {due_d.isoformat()}"
            checks.append({"protocol": "SR-03", "verdict": v, "due": due_d.isoformat(), "detail": d, "refs": refs})
            evidence.append(vd.evidence())
        # report classification vs effective classification
        for r in rep_row:
            f = self.src.execute("SELECT value_text FROM report_facts WHERE maintenance_id=? AND fact_key='stated_spill_class'", (r["maintenance_id"],)).fetchone()
            if f and f["value_text"] and cls not in ("unknown", f["value_text"]):
                flags.append(f"report {r['maintenance_id']} classed this spill '{f['value_text']}', but the current effective volume "
                             f"({vol:g} bbl) makes it '{cls}'; the report's classification and any duties it omitted are out of date")
        return {"checks": checks, "flags": flags, "evidence": _dedupe(evidence)}

    def nov_checks(self, asset_ids: list[str] | None, start: date | None, end: date) -> dict:
        resp = self.rule("rc04.nov_response_days")
        rows, evidence = [], []
        q = "SELECT * FROM reg_events WHERE event_type='notice_of_violation' AND date<=?"
        args: list[Any] = [end.isoformat()]
        if start:
            q += " AND date>=?"; args.append(start.isoformat())
        if asset_ids is not None:
            if not asset_ids:
                return {"rows": [], "evidence": []}
            q += f" AND asset_id IN ({','.join('?' * len(asset_ids))})"; args += list(asset_ids)
        for n in self.src.execute(q + " ORDER BY date", args):
            responses = self.src.execute(
                "SELECT * FROM reg_events WHERE asset_id=? AND event_type='nov_response_filed' AND date>=? AND "
                "(related_inspection_id IS ? OR related_inspection_id=?)",
                (n["asset_id"], n["date"], n["related_inspection_id"], n["related_inspection_id"])).fetchall()
            due = date.fromisoformat(n["date"]) + timedelta(days=resp.value) if resp and resp.value else None
            if responses:
                v = "met"
            elif due and self.as_of > due:
                v = "breached"
            else:
                v = "pending"
            rows.append({"protocol": "RC-04", "asset_id": n["asset_id"], "notice": n["event_id"], "notice_date": n["date"], "fine_usd": n["fine_usd"],
                         "notice_status": n["status"], "response_due": due.isoformat() if due else None, "verdict": v,
                         "responses": [r["event_id"] for r in responses],
                         "detail": f"notice {n['event_id']} ({n['date']}) needs a response within {resp.value:g} days (by {due}); "
                                   + ("response on record" if responses else "no response recorded") if resp else "no response-deadline rule found"})
            evidence.append(ev("reg_event", n["event_id"], f"{n['event_id']} notice_of_violation", {
                "date": n["date"], "fine_usd": n["fine_usd"], "status": n["status"], "detail": n["detail"]}, f"assets/{n['asset_id']}/regulatory-events.jsonl"))
        if resp:
            evidence.append(resp.evidence())
        return {"rows": rows, "evidence": _dedupe(evidence)}

    def asset_compliance(self, asset_id: str, window: TimeWindow, viewer: str | None = None) -> dict:
        """Everything the SOPs/manuals say this asset owes, judged from recorded evidence only.

        breaches: deadline passed and the record shows non-compliance (or no required record exists)
        pending:  duty open but deadline not passed, or action required that cannot be verified
        met:      requirement satisfied on record
        """
        a = self.asset(asset_id)
        if not a:
            return {"error": f"unknown asset {asset_id}"}
        start, end, label = window.resolve(self.as_of)
        out: dict[str, list] = {"breaches": [], "pending": [], "met": []}
        evidence: list[dict] = []

        def add(bucket, protocol, title, detail, date_=None, refs=None, extra=None):
            out[bucket].append({"protocol": protocol, "title": title, "detail": detail, "date": date_, "refs": refs or [], **(extra or {})})

        sp = self.spills([asset_id], window)
        for s in sp["rows"]:
            if s["voided"]:
                continue
            c = self.spill_compliance(s)
            evidence += c["evidence"]
            for k in c["checks"]:
                title = f"{k['protocol']} - spill {s['inspection_id']} ({s['date']}, {s['volume_bbl']:g} bbl, {s['class']})"
                bucket = {"breached": "breaches", "no_report_on_record_overdue": "breaches", "met": "met"}.get(k["verdict"], "pending")
                if k["verdict"] == "unknown":
                    bucket = "pending"
                add(bucket, k["protocol"], title, k["detail"], s["date"], k.get("refs"), {"verdict": k["verdict"], "inspection_id": s["inspection_id"]})
            for fl in c["flags"]:
                add("pending", "SR", f"Stale analysis on {s['inspection_id']}", fl, s["date"], [], {"verdict": "flag", "inspection_id": s["inspection_id"]})
        nv = self.nov_checks([asset_id], start, end)
        evidence += nv["evidence"]
        for n in nv["rows"]:
            add({"breached": "breaches", "met": "met"}.get(n["verdict"], "pending"), "RC-04", f"RC-04 - {n['notice']}", n["detail"],
                n["notice_date"], [f"reg_event:{n['notice']}"] + [f"reg_event:{r}" for r in n["responses"]],
                {"verdict": n["verdict"], "fine_usd": n["fine_usd"], "notice_status": n["notice_status"]})

        ins = self.eff_inspections([asset_id], None, self.as_of)
        # RC-01 inspection cadence
        gap_rule = self.rule("rc01.max_inspection_gap_days")
        if gap_rule and gap_rule.value and ins:
            evidence.append(gap_rule.evidence())
            prev = None
            gaps = []
            for i in ins:
                d_ = date.fromisoformat(i["date"])
                if prev is not None and (d_ - prev[0]).days > gap_rule.value and (start is None or d_ >= start) and d_ <= end:
                    gaps.append((prev, i, (d_ - prev[0]).days))
                prev = (d_, i)
            for p, i, n in gaps:
                add("breaches", "RC-01", f"RC-01 - {n}-day gap before {i['inspection_id']}",
                    f"no inspection between {p[0]} and {i['date']} ({n} days, limit {gap_rule.value:g})", i["date"],
                    [f"inspection:{p[1]['inspection_id']}", f"inspection:{i['inspection_id']}"], {"verdict": "breached"})
            age = (self.as_of - date.fromisoformat(ins[-1]["date"])).days
            if age > gap_rule.value:
                add("breaches", "RC-01", "RC-01 - overdue for inspection", f"last inspection {ins[-1]['date']} is {age} days old", ins[-1]["date"],
                    [f"inspection:{ins[-1]['inspection_id']}"], {"verdict": "breached"})
        # RC-03 downtime longer than N consecutive days
        r3 = self.rule("rc03.report_after_days") or self.rule("downtime.report_after_days", a["well_type"], asset_id)
        if r3 and r3.value:
            evidence.append(r3.evidence())
            run: list[dict] = []
            runs = []
            for i in ins + [None]:
                if i is not None and i["producing"] is False:
                    run.append(i); continue
                if run and 7 * len(run) > r3.value:
                    runs.append(run)
                run = []
            for rn in runs:
                if (start is None or date.fromisoformat(rn[-1]["date"]) >= start) and date.fromisoformat(rn[0]["date"]) <= end:
                    add("pending", "RC-03", f"RC-03 - {7 * len(rn)} consecutive days not producing ({rn[0]['date']} to {rn[-1]['date']})",
                        f"downtime above {r3.value:g} days must be reported to the area production supervisor; the dataset has no record to confirm the report was made",
                        rn[-1]["date"], [f"inspection:{x['inspection_id']}" for x in rn[:3]], {"verdict": "duty_triggered_unverifiable"})
        # RC-02 annual flowline integrity test, only for calendar years fully observed
        if ins:
            first = date.fromisoformat(ins[0]["date"])
            tests = [dict(r) for r in self.src.execute(
                "SELECT maintenance_id, started_at FROM maintenance WHERE asset_id=? AND lower(issue) LIKE '%flowline integrity test%'", (asset_id,))]
            by_year = defaultdict(list)
            for t in tests:
                by_year[int(t["started_at"][:4])].append(t)
            y0 = (start or first).year
            for y in range(y0, self.as_of.year + 1):
                ystart, yend = date(y, 1, 1), date(y, 12, 31)
                if start and yend < start or ystart > end:
                    continue
                if y == self.as_of.year:
                    if by_year.get(y):
                        add("met", "RC-02", f"RC-02 - flowline integrity test {y}", f"done {by_year[y][0]['started_at'][:10]} ({by_year[y][0]['maintenance_id']})",
                            by_year[y][0]["started_at"][:10], [f"maintenance:{by_year[y][0]['maintenance_id']}"], {"verdict": "met"})
                    else:
                        add("pending", "RC-02", f"RC-02 - flowline integrity test {y}", f"not yet done this year; due by {yend.isoformat()}", None, [], {"verdict": "pending"})
                elif first <= ystart + timedelta(days=14):  # year fully observed
                    if by_year.get(y):
                        add("met", "RC-02", f"RC-02 - flowline integrity test {y}", f"done {by_year[y][0]['started_at'][:10]} ({by_year[y][0]['maintenance_id']})",
                            by_year[y][0]["started_at"][:10], [f"maintenance:{by_year[y][0]['maintenance_id']}"], {"verdict": "met"})
                    else:
                        add("breaches", "RC-02", f"RC-02 - flowline integrity test {y}", f"no annual flowline integrity test recorded in {y}", f"{y}-12-31", [], {"verdict": "breached"})
        # RCA pending (manual section 10)
        rca = self.rca_status([asset_id])
        evidence += rca["evidence"]
        for r in rca["rows"]:
            if r["rca_required"]:
                add("pending", "RCA", f"Root-cause analysis due on {r['equipment']}",
                    f"{r['corrective_repairs']} corrective repairs on {r['equipment']} since {r['window_start']} (threshold {r['threshold']:g}, "
                    f"source: {r['threshold_rule']})", None, [f"maintenance:{m}" for m in r["maintenance_ids"]], {"verdict": "action_required"})
        # planned shut-in missing from the maintenance system (manual section 4: record before work starts)
        d0 = self.as_of.isoformat()
        for n in self.src.execute("SELECT * FROM notes WHERE asset_id=? AND note_type IN (?,?) AND (valid_from IS NULL OR valid_from<=?) "
                                  "AND (valid_to IS NULL OR valid_to>=?)", (asset_id, *STATUS_NOTE_TYPES, d0, d0)):
            if re.search(r"planned|work order", n["text"], re.I) and not INS_REF.search(n["text"]):
                vf = n["valid_from"] or d0
                vt = n["valid_to"] or d0
                m = self.src.execute("SELECT maintenance_id FROM maintenance WHERE asset_id=? AND started_at<=? AND (completed_at IS NULL OR completed_at>=?)",
                                     (asset_id, day_bounds(date.fromisoformat(vt), end=True), day_bounds(date.fromisoformat(vf)))).fetchone()
                if not m:
                    add("pending", "WS/Manual-4", "Planned shut-in not recorded in the maintenance system",
                        f"note {n['note_id']} describes a planned shut-in ({n['valid_from']} to {n['valid_to'] or 'open'}) but no maintenance record covers it; "
                        "the well-type manual requires recording a planned shut-in before work starts",
                        n["valid_from"], [f"note:{n['note_id']}"], {"verdict": "action_required"})
                    evidence.append(ev("note", n["note_id"], f"Note {n['note_id']}", {"text": n["text"], "author": self.user_name(n["author_user_id"])},
                                       f"assets/{asset_id}/context.jsonl", "note"))
        fines = [dict(r) for r in self.src.execute(
            "SELECT event_id, fine_usd, status, date FROM reg_events WHERE asset_id=? AND fine_usd>0 AND date<=?"
            + (" AND date>=?" if start else ""), [asset_id, end.isoformat()] + ([start.isoformat()] if start else []))]
        out["fines"] = {"total_usd": sum(f["fine_usd"] for f in fines), "open_usd": sum(f["fine_usd"] for f in fines if f["status"] == "open"), "events": fines}
        out["window"] = {"start": start.isoformat() if start else None, "end": end.isoformat(), "label": label}
        out["asset"] = a
        out["evidence"] = _dedupe(evidence)
        return out

    # ------------------------------------------------------------------ maintenance
    def maintenance(self, asset_ids: list[str] | None, window: TimeWindow, mtype: str | None = None,
                    equipment_kind: str | None = None) -> dict:
        start, end, label = window.resolve(self.as_of)
        q, args = "SELECT * FROM maintenance WHERE 1=1", []
        if asset_ids is not None:
            if not asset_ids:
                return {"rows": [], "evidence": [], "total_cost_usd": 0, "window": {"label": label}}
            q += f" AND asset_id IN ({','.join('?' * len(asset_ids))})"; args += list(asset_ids)
        if start:
            q += " AND started_at>=?"; args.append(day_bounds(start))
        q += " AND started_at<?"; args.append(day_bounds(end, end=True))
        if mtype:
            q += " AND maintenance_type=?"; args.append(mtype)
        if equipment_kind:
            q += " AND equipment_kind=?"; args.append(equipment_kind)
        q += " ORDER BY started_at"
        rows = []
        for m in self.src.execute(q, args):
            rows.append({"maintenance_id": m["maintenance_id"], "asset_id": m["asset_id"], "started_at": m["started_at"],
                         "completed_at": m["completed_at"], "type": m["maintenance_type"], "equipment": m["equipment_kind"],
                         "issue": m["issue"], "action": m["action_taken"], "downtime_hours": m["downtime_hours"], "cost_usd": m["cost_total"],
                         "labor_usd": m["cost_labor"], "parts_usd": m["cost_parts"], "report_file": m["report_file"],
                         "open": m["completed_at"] is None or m["completed_at"] > self.now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        total = round(sum(r["cost_usd"] or 0 for r in rows), 2)
        evidence = [ev("maintenance", r["maintenance_id"], f"{r['maintenance_id']} {r['issue']}", {
            "started": r["started_at"], "cost_usd": r["cost_usd"], "equipment": r["equipment"], "type": r["type"]},
            f"assets/{r['asset_id']}/maintenance.jsonl") for r in rows[:80]]
        return {"rows": rows, "total_cost_usd": total, "count": len(rows),
                "window": {"start": start.isoformat() if start else None, "end": end.isoformat(), "label": label},
                "method": "repairs belong to a period by their start date", "evidence": evidence}

    def rca_status(self, asset_ids: list[str] | None, equipment_kind: str | None = None) -> dict:
        assets = {a["asset_id"]: a for a in self.assets(asset_ids=asset_ids)} if asset_ids is not None else {a["asset_id"]: a for a in self.assets()}
        rows, evidence, rules_used = [], [], {}
        for aid, a in assets.items():
            thr = self.rule("rca.min_corrective_events", a["well_type"], aid)
            win = self.rule("rca.window_months", a["well_type"], aid)
            if not thr or not win or thr.value is None or win.value is None:
                continue
            from .timewin import add_months
            start = add_months(self.as_of, -int(win.value))
            q = ("SELECT * FROM maintenance WHERE asset_id=? AND maintenance_type='corrective' AND started_at>? AND started_at<?")
            args = [aid, day_bounds(start), day_bounds(self.as_of, end=True)]
            if equipment_kind:
                q += " AND equipment_kind=?"; args.append(equipment_kind)
            by_kind: dict[str, list] = defaultdict(list)
            for m in self.src.execute(q + " ORDER BY started_at", args):
                by_kind[m["equipment_kind"]].append(m)
            for kind, ms in by_kind.items():
                n = len(ms)
                rows.append({"asset_id": aid, "well_type": a["well_type"], "equipment": kind, "corrective_repairs": n,
                             "threshold": thr.value, "rca_required": n >= thr.value, "threshold_source": thr.origin,
                             "threshold_rule": thr.source_doc if thr.origin == "document" else f"{thr.override_id} by {self.user_name(thr.author)}",
                             "window_start": start.isoformat(), "maintenance_ids": [m["maintenance_id"] for m in ms]})
                if n >= thr.value:
                    evidence += [ev("maintenance", m["maintenance_id"], f"{m['maintenance_id']} {m['issue']}", {
                        "started": m["started_at"], "equipment": m["equipment_kind"]}, f"assets/{aid}/maintenance.jsonl") for m in ms]
                    rules_used[thr.source_doc + thr.section] = thr.evidence()
        evidence += list(rules_used.values())
        rows.sort(key=lambda r: (not r["rca_required"], -r["corrective_repairs"], r["asset_id"]))
        return {"rows": rows, "evidence": _dedupe(evidence)}

    # ------------------------------------------------------------------ aggregates
    def spill_heatmap(self, window: TimeWindow, area_ids: list[str] | None = None) -> dict:
        start, end, label = window.resolve(self.as_of)
        asset_ids = [a["asset_id"] for a in self.assets()]
        if area_ids:
            asset_ids = [a["asset_id"] for a in self.assets() if a["area_id"] in area_ids]
        sp = self.spills(asset_ids, window)
        months, areas = [], self.areas()
        cur = date((start or self.as_of).year, (start or self.as_of).month, 1)
        while cur <= end:
            months.append(cur.strftime("%Y-%m"))
            cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        grid = {aid: {m: 0 for m in months} for aid in areas}
        for r in sp["rows"]:
            if not r["voided"] and r["area"] in grid:
                grid[r["area"]][r["date"][:7]] += 1
        rows = []
        for aid, cols in grid.items():
            if area_ids and aid not in area_ids:
                continue
            rows.append({"area_id": aid, "area": areas[aid]["name"], **cols, "total": sum(cols.values())})
        return {"months": months, "rows": rows, "window": sp["window"], "voided_excluded": sp["voided"],
                "evidence": [ev("computed", "spill_heatmap", f"Spill counts by area and month, {label}", {
                    "detections": sp["detections"], "corrected_excluded": sp["voided"]}, None, "computed")]}

    def search_docs(self, query: str, kinds: list[str] | None = None, asset_id: str | None = None, limit: int = 6) -> dict:
        toks = [t for t in re.findall(r"[A-Za-z0-9_\-\.]{2,}", query)][:12]
        if not toks:
            return {"rows": [], "evidence": []}
        fts = " OR ".join('"' + t.replace('"', "") + '"' for t in toks)
        q = ("SELECT d.doc_id, d.kind, d.title, d.path, d.asset_id, d.maintenance_id, c.section, "
             "snippet(doc_chunks, 2, '[', ']', ' … ', 24) snip, bm25(doc_chunks) score "
             "FROM doc_chunks c JOIN documents d ON d.doc_id=c.doc_id WHERE doc_chunks MATCH ?")
        args: list[Any] = [fts]
        if kinds:
            q += f" AND d.kind IN ({','.join('?' * len(kinds))})"; args += kinds
        if asset_id:
            q += " AND (d.asset_id=? OR d.asset_id IS NULL)"; args.append(asset_id)
        q += " ORDER BY score LIMIT ?"; args.append(limit)
        try:
            rs = self.src.execute(q, args).fetchall()
        except sqlite3.OperationalError:
            return {"rows": [], "evidence": [], "warnings": ["search query could not be parsed"]}
        rows = [{"doc_id": r["doc_id"], "kind": r["kind"], "title": r["title"], "section": r["section"], "snippet": r["snip"],
                 "asset_id": r["asset_id"], "maintenance_id": r["maintenance_id"]} for r in rs]
        evidence = [ev("document", r["doc_id"], r["title"], {"section": r["section"], "snippet": r["snip"]}, r["path"], "document") for r in rs]
        return {"rows": rows, "evidence": _dedupe(evidence)}

    def notes(self, asset_id: str, viewer: str | None) -> dict:
        notes = [dict(r) for r in self.src.execute("SELECT * FROM notes WHERE asset_id=? ORDER BY created_at", (asset_id,))]
        claims = self.state.claims(asset_id, viewer)
        ev_list = [ev("note", n["note_id"], f"Note {n['note_id']}", {"author": self.user_name(n["author_user_id"]), "type": n["note_type"],
                                                                    "text": n["text"], "valid_from": n["valid_from"], "valid_to": n["valid_to"]},
                      f"assets/{asset_id}/context.jsonl", "note") for n in notes]
        ev_list += [ev("claim", c["claim_id"], f"Claim {c['claim_id']}", {"author": self.user_name(c["author_user_id"]), "text": c["text"],
                                                                      "verification": c["verification"], "visibility": c["visibility"]}, None, "claim") for c in claims]
        return {"notes": notes, "claims": claims, "evidence": ev_list}


def _dedupe(evs: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for e in evs:
        if e["ref"] not in seen:
            seen.add(e["ref"])
            out.append(e)
    return out


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]
