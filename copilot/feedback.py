"""Auditable feedback: corrections to raw data / AI analysis, scoped rule changes, and claims.

Design rules
* Source rows are never edited; corrections are overlays with author, reason, scope, timestamp.
* The original value is always kept. Competing corrections by different people are kept and flagged.
* Scope must be explicit. Well-type and global changes need a supervisory role and only touch
  company-policy rules; regulatory thresholds in the SOPs cannot be overridden by chat.
* Claims are checked against the latest inspection; contradictions are recorded, never hidden.
"""
from __future__ import annotations

import re
from datetime import date

from .domain import INSPECTION_FIELDS, Domain, ev

RAW_FIELDS = {"wellhead_temp_c", "ambient_temp_c", "spill_area_pct"}
ANALYSIS_FIELDS = {"spill_est_volume_bbl"}
DETECTION_FIELDS = {"spill_detected"}
FIELD_RANGE = {"wellhead_temp_c": (-90, 300), "ambient_temp_c": (-90, 70), "spill_est_volume_bbl": (0, 100000), "spill_area_pct": (0, 100)}
OVERRIDABLE_RULES = {"rca.min_corrective_events": (1, 20), "rca.window_months": (1, 60)}
PRIVILEGED_ROLES = {"production_supervisor", "hse_officer", "maintenance_lead"}
CLAIM_TYPES = {"planned_shutdown", "status_producing", "status_not_producing", "observation"}


class FeedbackError(ValueError):
    pass


class Feedback:
    def __init__(self, domain: Domain):
        self.d = domain
        self.state = domain.state

    # ------------------------------------------------------------------ helpers
    def _user(self, user_id: str) -> dict:
        u = self.d.users().get(user_id)
        if not u:
            raise FeedbackError(f"unknown user {user_id}")
        return u

    def find_inspection(self, asset_id: str, on_date: str | None = None, inspection_id: str | None = None) -> dict:
        if inspection_id:
            row = self.d.src.execute("SELECT inspection_id, asset_id FROM inspections WHERE inspection_id=?", (inspection_id,)).fetchone()
            if not row:
                raise FeedbackError(f"inspection {inspection_id} not found")
            return self.inspection(inspection_id)
        if not asset_id:
            raise FeedbackError("give an inspection_id, or an asset_id with a date")
        asset_id = asset_id.upper()
        if not self.d.asset(asset_id):
            raise FeedbackError(f"unknown asset {asset_id}")
        if on_date:
            rows = self.d.src.execute("SELECT inspection_id FROM inspections WHERE asset_id=? AND substr(inspected_at,1,10)=?", (asset_id, on_date)).fetchall()
            if len(rows) != 1:
                raise FeedbackError(f"{len(rows)} inspections for {asset_id} on {on_date}; give the inspection_id")
            return self.inspection(rows[0][0])
        r = self.d.src.execute("SELECT inspection_id FROM inspections WHERE asset_id=? ORDER BY inspected_at DESC LIMIT 1", (asset_id,)).fetchone()
        if not r:
            raise FeedbackError(f"{asset_id} has no inspections")
        return self.inspection(r[0])

    def inspection(self, inspection_id: str) -> dict:
        row = self.d.src.execute("SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)).fetchone()
        if not row:
            raise FeedbackError(f"inspection {inspection_id} not found")
        a = self.d.asset(row["asset_id"])
        void_notes, user = self.d._correction_maps()
        r = dict(row)
        r["well_type"], r["area_id"] = a["well_type"], a["area_id"]
        return self.d._effective(r, void_notes, user, {})

    def _snapshot(self, i: dict) -> dict:
        s = {"producing": i["producing"], "delta_c": i["delta_c"], "wellhead_temp_c": i["wellhead_temp_c"], "ambient_temp_c": i["ambient_temp_c"],
             "spill_volume_bbl": i["spill_est_volume_bbl"], "is_spill": i["is_spill"]}
        s["spill_class"] = self.d.spill_class(i["spill_est_volume_bbl"]) if i["is_spill"] else None
        return s

    # ------------------------------------------------------------------ corrections
    def correct_inspection(self, *, user_id: str, field: str, new_value, reason: str, asset_id: str | None = None,
                           on_date: str | None = None, inspection_id: str | None = None) -> dict:
        user = self._user(user_id)
        if field not in INSPECTION_FIELDS:
            raise FeedbackError(f"field '{field}' cannot be corrected; allowed: {sorted(INSPECTION_FIELDS)}")
        if not reason or len(reason.strip()) < 3:
            raise FeedbackError("a reason is required for every correction")
        ins = self.find_inspection(asset_id or "", on_date, inspection_id)
        if field == "spill_detected":
            nv = bool(new_value) if not isinstance(new_value, str) else new_value.strip().lower() in ("true", "yes", "1")
        else:
            try:
                nv = float(new_value)
            except (TypeError, ValueError):
                raise FeedbackError(f"{field} needs a numeric value") from None
            lo, hi = FIELD_RANGE[field]
            if not lo <= nv <= hi:
                raise FeedbackError(f"{field}={nv} is outside the plausible range {lo}..{hi}")
        if field in ("spill_est_volume_bbl", "spill_area_pct") and not ins["spill_detected_raw"] and not ins["applied_corrections"]:
            raise FeedbackError(f"{ins['inspection_id']} has no detected spill; correct 'spill_detected' first")
        before = self._snapshot(ins)
        original = ins["raw_values"][field]
        kind = "raw_value" if field in RAW_FIELDS else ("analysis_value" if field in ANALYSIS_FIELDS else "detection")
        # competing active corrections to the same field by someone else
        prior = self.state.corrections(target_type="inspection", target_id=ins["inspection_id"], field=field)
        conflicts = [{"correction_id": c["correction_id"], "author": c["author_user_id"], "new_value": c["new_value"]}
                     for c in prior if c["author_user_id"] != user_id and c["new_value"] != nv]
        supersedes = next((c["correction_id"] for c in reversed(prior) if c["author_user_id"] == user_id), None)
        c = self.state.add_correction(kind=kind, target_type="inspection", target_id=ins["inspection_id"], field=field,
                                      original_value=original, new_value=nv, scope_kind="inspection", scope_value=ins["inspection_id"],
                                      author_user_id=user_id, reason=reason, conflicts=conflicts, supersedes_id=supersedes)
        after_i = self.inspection(ins["inspection_id"])
        after = self._snapshot(after_i)
        impact = self._impact(ins, after_i, before, after)
        team_note = None
        a = self.d.asset(ins["asset_id"])
        if a and user.get("team_id") and a["team_id"] != user["team_id"]:
            team_note = f"{user['name']} is on {user['team_id']} but {a['asset_id']} belongs to {a['team_id']}; recorded, flagged for that team's review"
        return {"correction": c, "asset_id": ins["asset_id"], "inspection_id": ins["inspection_id"], "original_value": original,
                "before": before, "after": after, "impact": impact["items"], "conflicts": conflicts, "needs_confirmation": impact["needs_confirmation"],
                "warnings": ([team_note] if team_note else []) + ([f"competing correction by {self.d.user_name(x['author'])} ({x['correction_id']}) "
                                                                      f"has a different value; the most recent is applied" for x in conflicts]),
                "evidence": [self.d.inspection_evidence(after_i), ev("correction", c["correction_id"], f"Correction {c['correction_id']}", {
                    "field": field, "original": original, "new": nv, "author": user["name"], "reason": reason, "scope": f"inspection:{ins['inspection_id']}"},
                    None, "correction")] + impact["evidence"]}

    def _impact(self, ins: dict, after_i: dict, before: dict, after: dict) -> dict:
        items, evidence, confirm = [], [], []
        if before["producing"] != after["producing"]:
            items.append(f"production status for this reading changes from "
                         f"{_yn(before['producing'])} to {_yn(after['producing'])} "
                         f"({after['delta_c']} C above ambient vs threshold {after_i['threshold_c']} C)")
        if before["spill_class"] != after["spill_class"] and after["is_spill"]:
            items.append(f"spill classification changes from {before['spill_class']} to {after['spill_class']} "
                         f"({before['spill_volume_bbl']} -> {after['spill_volume_bbl']} bbl)")
            spill = next((s for s in self.d.spills([after_i["asset_id"]], _day_window(after_i["date"]))["rows"]
                          if s["inspection_id"] == after_i["inspection_id"]), None)
            if spill:
                comp = self.d.spill_compliance(spill)
                evidence += comp["evidence"]
                for k in comp["checks"]:
                    items.append(f"{k['protocol']}: {k['verdict']} - {k['detail']}")
                items += comp["flags"]
        if after_i["notes"] and after["producing"] is True and re.search(r"not moving|stopped|not running|tripped|idle", after_i["notes"], re.I):
            msg = (f"the same inspection carries the inspector's note \"{after_i['notes']}\" "
                   f"({self.d.user_name(after_i['inspector_user_id'])}), which disagrees with a producing reading")
            items.append(msg)
            confirm.append(msg)
        # other users' formal sources that disagree
        if ins["spill_voided"] and after["is_spill"]:
            items.append("the dataset's correction note had voided this detection; it is now counted again")
        return {"items": items, "evidence": evidence, "needs_confirmation": confirm}

    def withdraw(self, user_id: str, correction_id: str, reason: str | None) -> dict:
        self._user(user_id)
        c = self.state.get_correction(correction_id)
        if not c:
            raise FeedbackError("correction not found")
        if c["author_user_id"] != user_id and self.d.users()[user_id]["role"] not in PRIVILEGED_ROLES:
            raise FeedbackError("only the author or a supervisor/HSE/maintenance lead can withdraw a correction")
        return self.state.withdraw_correction(correction_id, user_id, reason)

    # ------------------------------------------------------------------ rule overrides
    def set_rule_override(self, *, user_id: str, rule_key: str, value: float, scope: str, scope_value: str, reason: str) -> dict:
        user = self._user(user_id)
        if rule_key not in OVERRIDABLE_RULES:
            raise FeedbackError(f"rule '{rule_key}' cannot be changed from chat. Overridable company-policy rules: "
                                f"{sorted(OVERRIDABLE_RULES)}. Regulatory and production-definition thresholds come from the manuals/SOPs.")
        lo, hi = OVERRIDABLE_RULES[rule_key]
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise FeedbackError("rule value must be numeric") from None
        if not lo <= value <= hi:
            raise FeedbackError(f"{rule_key} must be between {lo} and {hi}")
        if scope not in ("well_type", "asset"):
            raise FeedbackError("scope must be 'well_type' or 'asset' (a fleet-wide scope is not accepted from chat)")
        if scope == "well_type":
            scope_value = scope_value.upper()
            if scope_value not in self.d.well_types():
                raise FeedbackError(f"unknown well type {scope_value}; known: {self.d.well_types()}")
            if user["role"] not in PRIVILEGED_ROLES:
                raise FeedbackError(f"role '{user['role']}' cannot change rules for a whole well type (needs supervisor, HSE or maintenance lead)")
        else:
            scope_value = scope_value.upper()
            if not self.d.asset(scope_value):
                raise FeedbackError(f"unknown asset {scope_value}")
        base = self.d._doc_rule(rule_key, self.d.asset(scope_value)["well_type"] if scope == "asset" else scope_value)
        prior = self.state.corrections(kind="rule_override", field=rule_key, target_type=scope, target_id=scope_value)
        conflicts = [{"correction_id": c["correction_id"], "author": c["author_user_id"], "new_value": c["new_value"]}
                     for c in prior if c["author_user_id"] != user_id and float(c["new_value"]) != value]
        c = self.state.add_correction(kind="rule_override", target_type=scope, target_id=scope_value, field=rule_key,
                                      original_value=base.value if base else None, new_value=value, scope_kind=scope, scope_value=scope_value,
                                      author_user_id=user_id, reason=reason,
                                      conflicts=conflicts, supersedes_id=prior[-1]["correction_id"] if prior and prior[-1]["author_user_id"] == user_id else None)
        return {"correction": c, "rule_key": rule_key, "original": base.value if base else None, "new": value, "scope": f"{scope}:{scope_value}",
                "source_of_original": f"{base.source_doc} {base.section}" if base else None, "conflicts": conflicts,
                "note": "applied only at this scope; every other well type/asset keeps the manual's value",
                "evidence": [ev("correction", c["correction_id"], f"Rule override {c['correction_id']}", {
                    "rule": rule_key, "original": base.value if base else None, "new": value, "author": user["name"], "scope": f"{scope}:{scope_value}",
                    "reason": reason}, None, "correction")] + ([base_ev] if (base_ev := (base.evidence() if base else None)) else [])}

    # ------------------------------------------------------------------ claims
    def add_claim(self, *, user_id: str, asset_id: str, text: str, claim_type: str, valid_from: str | None = None,
                  valid_to: str | None = None, visibility: str = "shared") -> dict:
        user = self._user(user_id)
        asset_id = asset_id.upper()
        a = self.d.asset(asset_id)
        if not a:
            raise FeedbackError(f"unknown asset {asset_id}")
        if claim_type not in CLAIM_TYPES:
            raise FeedbackError(f"claim_type must be one of {sorted(CLAIM_TYPES)}")
        if visibility not in ("shared", "private"):
            raise FeedbackError("visibility must be 'shared' or 'private'")
        for d_ in (valid_from, valid_to):
            if d_:
                try:
                    date.fromisoformat(d_)
                except ValueError:
                    raise FeedbackError(f"invalid date '{d_}' (use YYYY-MM-DD)") from None
        verdict, detail, evidence = self.assess_claim(asset_id, claim_type, valid_from, valid_to)
        # disagreement with other users' recorded claims
        others = [c for c in self.state.claims(asset_id, user_id) if c["author_user_id"] != user_id and c["visibility"] == "shared"
                  and _opposes(c["claim_type"], claim_type) and _overlap(c, valid_from, valid_to, self.d.as_of.isoformat())]
        extra = []
        if others:
            extra.append("conflicts with " + ", ".join(f"{c['claim_id']} by {self.d.user_name(c['author_user_id'])}" for c in others))
            detail = (detail + "; " if detail else "") + extra[0]
        c = self.state.add_claim(asset_id=asset_id, author_user_id=user_id, text=text, claim_type=claim_type, valid_from=valid_from,
                                 valid_to=valid_to, visibility=visibility, verification=verdict, verification_detail=detail)
        for o in others:
            self.state.audit(user_id, "claim_conflict", "claim", c["claim_id"], {"conflicts_with": o["claim_id"]})
        return {"claim": c, "verification": verdict, "detail": detail, "conflicting_claims": [o["claim_id"] for o in others],
                "needs_confirmation": verdict in ("unverified", "contradicted") or bool(others),
                "evidence": evidence + [ev("claim", c["claim_id"], f"Claim {c['claim_id']}", {
                    "author": user["name"], "text": text, "verification": verdict, "visibility": visibility}, None, "claim")]}

    def assess_claim(self, asset_id: str, claim_type: str, valid_from: str | None, valid_to: str | None):
        st = self.d.status(asset_id)
        evidence = st.get("evidence", [])
        li = st.get("latest")
        if claim_type == "observation" or not li or li["producing"] is None:
            return "unverified", "no inspection evidence to check this against", evidence
        today = self.d.as_of.isoformat()
        start = valid_from or today
        if start > today and claim_type == "planned_shutdown":
            return "unverified", f"future plan starting {start}; nothing in the data can confirm it yet", evidence
        state = "producing" if li["producing"] else "not producing oil"
        base = (f"latest inspection {li['inspection_id']} ({li['date']}) shows the well {state} "
                f"({li['delta_c']} C above ambient, threshold {li['threshold_c']} C)")
        if claim_type in ("planned_shutdown", "status_not_producing"):
            if li["producing"] is False:
                return "supported", base + "; consistent with the claim", evidence
            return "contradicted", base + "; the claim says the well is stopped", evidence
        if claim_type == "status_producing":
            if li["producing"] is True:
                return "supported", base + "; consistent with the claim", evidence
            extra = ""
            if st["explanations"]:
                extra = "; " + st["explanations"][0]["text"]
            return "contradicted", base + "; the claim says the well is producing" + extra, evidence
        return "unverified", base, evidence


def _yn(v) -> str:
    return "unknown" if v is None else ("producing" if v else "not producing")


def _opposes(a: str, b: str) -> bool:
    stopped = {"planned_shutdown", "status_not_producing"}
    return (a in stopped and b == "status_producing") or (b in stopped and a == "status_producing")


def _overlap(c: dict, vf: str | None, vt: str | None, today: str) -> bool:
    s1, e1 = c["valid_from"] or "0000", c["valid_to"] or "9999"
    s2, e2 = vf or today, vt or "9999"
    return s1 <= e2 and s2 <= e1


def _day_window(day: str):
    from .timewin import TimeWindow
    d = date.fromisoformat(day)
    return TimeWindow(start_date=d, end_date=d)
