"""Corrections, rule scope, claims, memory isolation and disagreement handling (Q12-Q18).

Expected behaviour is taken from dataset/03-evaluation-queries.md. Each test starts from a fresh state.db.
"""
from __future__ import annotations

import pytest

from copilot.feedback import FeedbackError
from copilot.timewin import TimeWindow

from .conftest import CHRIS, JAKE, JESSICA, LAURA, MICHAEL, TYLER

NE7, NE8 = "Northeast Block 7", "Northeast Block 8"


def area_ids(dom, area):
    return [a["asset_id"] for a in dom.assets(area=area)]


def not_producing_ne8(dom, viewer=None):
    return [r["asset_id"] for r in dom.fleet_status(area=NE8, only="not_producing", viewer=viewer)["rows"]]


# ------------------------------------------------------------------ Q12
def test_q12_raw_data_correction_keeps_original_and_flips_status(dom, fb, state, source_conn):
    r = fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=41.0,
                              reason="handheld thermal gun was faulty; crew re-measured 41.0 C")
    assert r["inspection_id"] == "INS-0164-0205" and r["original_value"] == 24.0
    assert (r["before"]["producing"], r["after"]["producing"], r["after"]["delta_c"]) == (False, True, 21.1)
    c = r["correction"]
    assert (c["author_user_id"], c["status"], c["scope_kind"], c["scope_value"]) == (MICHAEL, "active", "inspection", "INS-0164-0205")
    assert "faulty" in c["reason"] and c["original_value"] == 24.0 and c["new_value"] == 41.0
    # disagreement with the inspector's own note is surfaced and confirmation requested
    assert any("Pump jack not moving." in m and "Tyler Hughes" in m for m in r["needs_confirmation"])
    # the source record is untouched
    row = source_conn.execute("SELECT wellhead_temp_c FROM inspections WHERE inspection_id='INS-0164-0205'").fetchone()
    assert row[0] == 24.0
    # audit trail records who/what/why
    trail = state.audit_trail("correction", c["correction_id"])
    assert trail and trail[0]["action"] == "correction_created" and trail[0]["actor_user_id"] == MICHAEL
    # Q2 again -> three wells, WELL-0164 gone
    assert not_producing_ne8(dom) == ["WELL-0170", "WELL-0172", "WELL-0175"]


# ------------------------------------------------------------------ Q13
def test_q13_analysis_correction_reclassifies_and_flags_duties(dom, fb):
    r = fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0151", on_date="2026-09-24", field="spill_est_volume_bbl", new_value=5.4,
                              reason="field crew measured 5.4 bbl")
    assert r["inspection_id"] == "INS-0151-0201" and r["original_value"] == 4.5   # AI estimate kept for reference
    assert (r["before"]["spill_class"], r["after"]["spill_class"]) == ("moderate", "reportable")
    spill = next(s for s in dom.spills(["WELL-0151"], TimeWindow(last_n=90, unit="days"))["rows"] if s["inspection_id"] == "INS-0151-0201")
    assert (spill["volume_bbl"], spill["original_volume_bbl"], spill["class"]) == (5.4, 4.5, "reportable")
    comp = dom.spill_compliance(spill)
    checks = {k["protocol"]: k for k in comp["checks"]}
    assert checks["SR-01"]["verdict"] == "no_report_on_record_overdue" and checks["SR-01"]["due"] == "2026-09-25T15:30:00Z"
    assert checks["SR-02"]["verdict"] == "breached" and checks["SR-02"]["actual_hours"] == 6.4
    assert checks["SR-03"]["verdict"] == "pending" and checks["SR-03"]["due"] == "2026-11-23"
    assert any("MNT-0151-025" in f and "'moderate'" in f for f in comp["flags"])   # report classification now stale
    assert any("SR-01" in i for i in r["impact"])


def test_without_correction_wells_0151_is_not_reportable(dom):
    s = next(s for s in dom.spills(["WELL-0151"], TimeWindow(last_n=90, unit="days"))["rows"] if s["inspection_id"] == "INS-0151-0201")
    assert s["class"] == "moderate" and s["original_volume_bbl"] is None
    assert [k["protocol"] for k in dom.spill_compliance(s)["checks"]] == ["SR-02"]   # no SR-01/SR-03 duties at 4.5 bbl


# ------------------------------------------------------------------ Q14
def test_q14_well_type_rule_applies_at_that_scope_only(dom, fb):
    base = {r["asset_id"] for r in dom.rca_status(area_ids(dom, NE7), "pump_jack")["rows"] if r["rca_required"]}
    assert base == {"WELL-0108", "WELL-0121", "WELL-0138"}
    ov = fb.set_rule_override(user_id=MICHAEL, rule_key="rca.min_corrective_events", value=2, scope="well_type", scope_value="T350",
                              reason="team now wants pump jack RCA after 2 repairs")
    assert ov["original"] == 3.0 and "T200.md" in ov["source_of_original"]
    rows = dom.rca_status(area_ids(dom, NE7), "pump_jack")["rows"]
    now = {r["asset_id"] for r in rows if r["rca_required"]}
    assert now == {"WELL-0108", "WELL-0120", "WELL-0121", "WELL-0138"}
    assert "WELL-0102" not in now                                    # T200 with 2 repairs must NOT be added
    src = {r["asset_id"]: r["threshold_source"] for r in rows if r["rca_required"]}
    assert src["WELL-0120"] == "user_override" and src["WELL-0121"] == "document"
    assert dom.rule("rca.min_corrective_events", "T200").value == 3.0   # other type untouched
    # another user (Jessica) sees the same rule, with author attribution available
    assert dom.rule("rca.min_corrective_events", "T350").author == MICHAEL


def test_q14_role_and_regulatory_gates(fb):
    with pytest.raises(FeedbackError, match="cannot change rules for a whole well type"):
        fb.set_rule_override(user_id=TYLER, rule_key="rca.min_corrective_events", value=2, scope="well_type", scope_value="T350", reason="x")
    with pytest.raises(FeedbackError, match="cannot be changed from chat"):
        fb.set_rule_override(user_id=MICHAEL, rule_key="sr01.report_hours", value=48, scope="well_type", scope_value="T350", reason="x")
    with pytest.raises(FeedbackError, match="unknown well type"):
        fb.set_rule_override(user_id=MICHAEL, rule_key="rca.min_corrective_events", value=2, scope="well_type", scope_value="T999", reason="x")
    with pytest.raises(FeedbackError, match="fleet-wide"):
        fb.set_rule_override(user_id=MICHAEL, rule_key="rca.min_corrective_events", value=2, scope="global", scope_value="*", reason="x")


def test_correction_validation(fb):
    kw = dict(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15")
    with pytest.raises(FeedbackError, match="reason"):
        fb.correct_inspection(field="wellhead_temp_c", new_value=41, reason="", **kw)
    with pytest.raises(FeedbackError, match="plausible range"):
        fb.correct_inspection(field="wellhead_temp_c", new_value=900, reason="typo", **kw)
    with pytest.raises(FeedbackError, match="cannot be corrected"):
        fb.correct_inspection(field="inspector_user_id", new_value="U-1", reason="not allowed", **kw)
    with pytest.raises(FeedbackError, match="numeric"):
        fb.correct_inspection(field="wellhead_temp_c", new_value="hot", reason="not a number", **kw)
    with pytest.raises(FeedbackError, match="unknown user"):
        fb.correct_inspection(user_id="U-999", asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=41, reason="unknown person")
    with pytest.raises(FeedbackError, match="no detected spill"):
        fb.correct_inspection(field="spill_est_volume_bbl", new_value=3, reason="no spill here", **kw)


def test_competing_corrections_are_flagged_and_most_recent_applies(dom, fb):
    kw = dict(asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", reason="re-measured")
    fb.correct_inspection(user_id=MICHAEL, new_value=41.0, **kw)
    r = fb.correct_inspection(user_id=JESSICA, new_value=30.0, **kw)
    assert [c["author"] for c in r["conflicts"]] == [MICHAEL] and any("competing" in w for w in r["warnings"])
    st = dom.status("WELL-0164")
    assert st["latest"]["wellhead_temp_c"] == 30.0 and st["latest"]["correction_conflicts"]
    assert any("competing" in w for w in st["warnings"])


def test_withdrawn_correction_stops_applying(dom, fb):
    r = fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=41.0, reason="re-measured")
    assert dom.status("WELL-0164")["producing"] is True
    with pytest.raises(FeedbackError):
        fb.withdraw(TYLER, r["correction"]["correction_id"], "no")      # unrelated field operator cannot withdraw
    fb.withdraw(MICHAEL, r["correction"]["correction_id"], "mistake")
    assert dom.status("WELL-0164")["producing"] is False


# ------------------------------------------------------------------ Q15
def test_q15_correction_reaches_other_users(dom, fb):
    fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=41.0, reason="faulty gun")
    fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0151", on_date="2026-09-24", field="spill_est_volume_bbl", new_value=5.4, reason="crew measured")
    # Jessica asks about WELL-0164
    s = dom.status("WELL-0164", viewer=JESSICA)
    assert s["producing"] is True
    corr = s["latest"]["applied_corrections"][0]
    assert corr["author_name"] == "Michael Turner" and corr["original"] == 24.0 and corr["new"] == 41.0
    assert "Pump jack not moving." in s["observation_conflict"]
    # Laura asks which NE-8 spills in 90 days needed a regulator report
    sp = dom.spills(area_ids(dom, NE8), TimeWindow(last_n=90, unit="days"))
    need = [(r["asset_id"], r["volume_bbl"]) for r in sp["rows"] if not r["voided"] and r["class"] in ("reportable", "major")]
    assert need == [("WELL-0151", 5.4)]
    c = next(k for k in dom.spill_compliance(next(r for r in sp["rows"] if r["asset_id"] == "WELL-0151"))["checks"] if k["protocol"] == "SR-01")
    assert c["verdict"] == "no_report_on_record_overdue"


# ------------------------------------------------------------------ Q16
def test_q16_shared_claim_visible_private_memory_is_not(dom, fb, state):
    c = fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="WELL-0007 will be shut in from 17 Oct for a 10-day planned workover",
                     claim_type="planned_shutdown", valid_from="2026-10-17", valid_to="2026-10-27")
    assert c["verification"] == "unverified" and c["needs_confirmation"]       # future plan: nothing can confirm it yet
    seen = state.claims("WELL-0007", MICHAEL)                                  # Michael, later, in a new chat
    assert [(x["author_user_id"], x["valid_from"], x["valid_to"]) for x in seen] == [(JAKE, "2026-10-17", "2026-10-27")]
    assert dom.notes("WELL-0007", MICHAEL)["claims"][0]["text"].startswith("WELL-0007 will be shut in")
    # Michael's earlier private memory (Q11) must never be visible to Jessica
    state.remember(user_id=MICHAEL, conversation_id="CONV-day1", topic="downtime", question="Which 3 wells in NE-7 had the most downtime?",
                   entity_ids=["WELL-0121", "WELL-0138", "WELL-0107"], window={"last_n": 4, "unit": "months"}, tool_name="get_downtime")
    assert state.recall(MICHAEL)[0]["entity_ids"] == ["WELL-0121", "WELL-0138", "WELL-0107"]
    assert state.recall(JESSICA) == []


def test_private_claims_and_conversations_are_isolated(fb, state):
    fb.add_claim(user_id=JESSICA, asset_id="WELL-0007", text="private reminder: check pump", claim_type="observation", visibility="private")
    assert [c["text"] for c in state.claims("WELL-0007", JESSICA)] == ["private reminder: check pump"]
    assert state.claims("WELL-0007", MICHAEL) == []
    conv = state.ensure_conversation(JESSICA, None, "mine")
    state.add_message(conv, JESSICA, "user", "hello")
    with pytest.raises(PermissionError):
        state.ensure_conversation(MICHAEL, conv)
    assert state.history(conv, MICHAEL) == []


# ------------------------------------------------------------------ Q17
def test_q17_conflicting_claims_are_recorded_and_flagged(dom, fb, state):
    # Prior claim (Q16) consumes CLM-0001; ids are sequential and global.
    first = fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="shut in from 17 Oct", claim_type="planned_shutdown", valid_from="2026-10-17")
    assert first["claim"]["claim_id"] == "CLM-0001"
    mt = fb.add_claim(user_id=MICHAEL, asset_id="WELL-0175", text="WELL-0175 has been shut in for planned work since 14 Oct",
                      claim_type="planned_shutdown", valid_from="2026-10-14")
    assert mt["claim"]["claim_id"] == "CLM-0002" and mt["verification"] == "supported"
    assert "INS-0175-0204" in mt["detail"] and "0.4" in mt["detail"]
    th = fb.add_claim(user_id=TYLER, asset_id="WELL-0175", text="WELL-0175 is running fine", claim_type="status_producing")
    assert th["claim"]["claim_id"] == "CLM-0003"
    assert th["verification"] == "contradicted" and th["needs_confirmation"]            # recorded as unconfirmed
    assert th["conflicting_claims"] == ["CLM-0002"]                                      # opposes Michael's claim, not Jake's WELL-0007 claim
    assert "0.4 C above ambient" in th["detail"] and "INS-0175-0204" in th["detail"]
    assert len(state.claims("WELL-0175", JESSICA)) == 2                                  # both statements stay on record
    jk = fb.add_claim(user_id=JAKE, asset_id="WELL-0001", text="WELL-0001 is back online since 14 Oct", claim_type="status_producing", valid_from="2026-10-14")
    assert jk["claim"]["claim_id"] == "CLM-0004" and jk["verification"] == "contradicted" and jk["needs_confirmation"]
    assert "INS-0001-0203" in jk["detail"] and "CTX-0001-003" in jk["detail"] and "Expected back online around 20 Oct" in jk["detail"]
    assert jk["conflicting_claims"] == []                                                # no other user's claim about WELL-0001
    # nothing was silently picked: the data-derived status is unchanged by any claim
    assert dom.status("WELL-0175")["producing"] is False and dom.status("WELL-0001")["producing"] is False
    trail = [a["action"] for a in state.audit_trail("claim", "CLM-0003")]
    assert "claim_created" in trail and "claim_conflict" in trail


def test_claim_ids_never_collide(fb):
    ids = [fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text=f"obs {i}", claim_type="observation")["claim"]["claim_id"] for i in range(3)]
    assert ids == ["CLM-0001", "CLM-0002", "CLM-0003"]


def test_claim_validation(fb):
    with pytest.raises(FeedbackError, match="unknown asset"):
        fb.add_claim(user_id=JAKE, asset_id="WELL-9999", text="x", claim_type="observation")
    with pytest.raises(FeedbackError, match="claim_type"):
        fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="x", claim_type="rumour")
    with pytest.raises(FeedbackError, match="invalid date"):
        fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="x", claim_type="observation", valid_from="17/10/2026")


# ------------------------------------------------------------------ Q18
def test_q18_disputed_spill_detection_stands_with_both_notes(dom):
    sp = dom.spills(["WELL-0101"], TimeWindow(start_date="2026-08-01", end_date="2026-08-31"))
    r = next(x for x in sp["rows"] if x["inspection_id"] == "INS-0101-0197")
    assert not r["voided"] and r["volume_bbl"] == 1.0 and r["date"] == "2026-08-20"      # no formal correction -> detection stands
    assert r["repairs"] == ["MNT-0101-029"]                                                # a spill repair was done
    notes = {n["note_id"]: n for n in r["informal_notes"]}
    assert set(notes) == {"CTX-0101-002", "CTX-0101-003"}
    assert notes["CTX-0101-002"]["author_name"] == "Ryan Brooks" and "produced water" in notes["CTX-0101-002"]["text"]
    assert notes["CTX-0101-003"]["author_name"] == "Chris Dalton" and notes["CTX-0101-003"]["role"] == "hse_officer"
    assert "crude oil" in notes["CTX-0101-003"]["text"]
    assert {"note:CTX-0101-002", "note:CTX-0101-003"} <= {e["ref"] for e in sp["evidence"]}
    # neither informal note changed the count
    assert sp["count"] == 1 and not any(a["field"] for a in dom.eff_inspections(["WELL-0101"], None, None)[0]["applied_corrections"])


def test_dataset_correction_note_wins_over_detection(dom):
    i = next(x for x in dom.eff_inspections(["WELL-0001"], None, None) if x["inspection_id"] == "INS-0001-0070")
    assert i["spill_detected_raw"] is True and i["is_spill"] is False and i["spill_voided"]["note_id"] == "CTX-0001-001"
