"""Deterministic checks of the domain layer against the expected answers in dataset/03-evaluation-queries.md.

Expected values are transcribed from that document (Q1-Q11, Q14 base case); none are derived from this code.
Q12-Q18 live in test_feedback_eval.py because they exercise state changes.
"""
from __future__ import annotations

import pytest

from copilot.timewin import TimeWindow

NE7, NE8, SW24 = "Northeast Block 7", "Northeast Block 8", "Southwest Block 24"


def ids(dom, area):
    return [a["asset_id"] for a in dom.assets(area=area)]


def test_as_of_is_challenge_today(dom):
    assert dom.as_of.isoformat() == "2026-10-16"


def test_users_match_challenge_roster(dom):
    names = {u["name"]: k for k, u in dom.users().items()}
    assert names["Michael Turner"] == "U-201" and names["Jessica Reed"] == "U-202" and names["Tyler Hughes"] == "U-204"
    assert names["Jake Morales"] == "U-102" and names["Laura Simmons"] == "U-401" and names["Chris Dalton"] == "U-402"


def test_rules_extracted_with_provenance(dom):
    r = dom.rule("production.min_delta_c", "T200")
    assert (r.value, r.origin) == (15.0, "document") and "T200.md" in r.source_doc
    assert dom.rule("production.min_delta_c", "T350").value == 20.0
    assert dom.rule("rca.min_corrective_events", "T350").value == 3.0  # inherited from T200 manual
    assert dom.rule("sr01.report_hours").value == 24 and dom.rule("sr02.contain_hours").value == 4
    assert dom.rule("sr03.verify_days").value == 60 and dom.rule("spill.class.reportable_min_bbl").value == 5


# ------------------------------------------------------------------ Q1
def test_q1_status_card_well_0001(dom):
    s = dom.status("WELL-0001")
    li = s["latest"]
    assert s["producing"] is False
    assert (li["inspection_id"], li["inspected_at"][:10], li["method"]) == ("INS-0001-0203", "2026-10-15", "drone")
    assert dom.user_name(li["inspector_user_id"]) == "Ryan Brooks"
    assert s["delta_c"] == 3.2 and s["threshold"] == 15.0
    assert [e["id"] for e in s["explanations"]] == ["CTX-0001-003"]   # planned shut-in; counterweight note is not a reason
    assert s["recorded_status"] == "active" and s["stale_recorded_status"]
    assert not s["no_recorded_reason"]


# ------------------------------------------------------------------ Q2
def test_q2_ne8_not_producing(dom):
    f = dom.fleet_status(area=NE8, only="not_producing")
    by = {r["asset_id"]: r for r in f["rows"]}
    assert set(by) == {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"}
    assert {k: v["delta_c"] for k, v in by.items()} == {"WELL-0164": 4.1, "WELL-0170": 2.9, "WELL-0172": 5.7, "WELL-0175": 0.4}
    assert {k: v["recorded_status"] for k, v in by.items()} == {"WELL-0164": "active", "WELL-0170": "down", "WELL-0172": "active", "WELL-0175": "active"}
    assert by["WELL-0164"]["no_recorded_reason"] and by["WELL-0175"]["no_recorded_reason"]
    assert by["WELL-0164"]["inspector_observation"] == "Pump jack not moving."
    assert by["WELL-0170"]["reason_refs"] == ["maintenance:MNT-0170-031"]
    assert by["WELL-0172"]["reason_refs"] == ["maintenance:MNT-0172-028"]
    assert all(r["inspected_at"][:10] == "2026-10-15" for r in f["rows"])
    assert {r["asset_id"] for r in f["rows"] if r["stale_recorded_status"]} == {"WELL-0164", "WELL-0172", "WELL-0175"}


# ------------------------------------------------------------------ Q3
def test_q3_thirteen_weeks_series(dom):
    p = dom.producing_series("WELL-0001", TimeWindow(last_n=13, unit="weeks"))["points"]
    assert len(p) == 13 and p[0]["date"] == "2026-07-23" and p[-1]["date"] == "2026-10-15"
    assert [x["date"] for x in p if x["producing"] is False] == ["2026-10-08", "2026-10-15"]


# ------------------------------------------------------------------ Q4
def test_q4_spills_well_0001_four_years(dom):
    sp = dom.spills(["WELL-0001"], TimeWindow(last_n=4, unit="years"), include_voided=True)
    assert sp["count"] == 6 and sp["total_bbl"] == 15.1 and sp["detections"] == 7 and sp["voided"] == 1
    assert [(r["date"], r["volume_bbl"]) for r in sp["rows"] if not r["voided"]] == [
        ("2023-03-16", 0.5), ("2023-04-27", 2.6), ("2023-09-28", 3.3), ("2023-10-19", 0.3), ("2026-06-11", 6.8), ("2026-10-01", 1.6)]
    v = next(r for r in sp["rows"] if r["voided"])
    assert (v["inspection_id"], v["void_source"], v["date"]) == ("INS-0001-0070", "CTX-0001-001", "2024-03-07")


# ------------------------------------------------------------------ Q5
def test_q5_downtime_2025(dom):
    r = dom.downtime(["WELL-0001"], TimeWindow(calendar_year=2025))["rows"][0]
    assert r["downtime_days"] == 21 and r["dates"] == ["2025-07-24", "2025-10-16", "2025-10-23"]
    hot = next(i for i in dom.eff_inspections(["WELL-0001"], None, None) if i["date"] == "2025-07-24")
    assert (hot["wellhead_temp_c"], hot["ambient_temp_c"], hot["delta_c"], hot["producing"]) == (51.6, 43.1, 8.5, False)  # hot day, still idle


# ------------------------------------------------------------------ Q6
def test_q6_top_downtime_ne7(dom):
    rows = dom.downtime(ids(dom, NE7), TimeWindow(last_n=4, unit="months"))["rows"]
    assert (dom.downtime(ids(dom, NE7), TimeWindow(last_n=4, unit="months"))["window"]["start"]) == "2026-06-16"
    top = {r["asset_id"]: (r["downtime_days"], r["well_type"]) for r in rows[:3]}
    assert top == {"WELL-0121": (49, "T200"), "WELL-0138": (42, "T350"), "WELL-0107": (42, "T350")}
    assert rows[3]["asset_id"] == "WELL-0119" and rows[3]["downtime_days"] == 28


# ------------------------------------------------------------------ Q7
def test_q7_map_spills_ne8_90_days(dom):
    sp = dom.spills(ids(dom, NE8), TimeWindow(last_n=90, unit="days"))
    got = {r["asset_id"]: (r["date"], r["volume_bbl"], r["lat"], r["lon"]) for r in sp["rows"] if not r["voided"]}
    assert got == {
        "WELL-0151": ("2026-09-24", 4.5, 32.12246, -102.18445),
        "WELL-0165": ("2026-07-30", 1.2, 32.15665, -102.14522),
        "WELL-0175": ("2026-07-30", 3.9, 32.18020, -102.14790),
        "WELL-0186": ("2026-07-23", 0.6, 32.17552, -102.17775)}


# ------------------------------------------------------------------ Q8
def test_q8_heatmap_2026(dom):
    h = dom.spill_heatmap(TimeWindow(calendar_year=2026))
    assert h["months"] == [f"2026-{m:02d}" for m in range(1, 11)]
    got = {r["area"]: ([r[m] for m in h["months"]], r["total"]) for r in h["rows"]}
    assert got == {
        "Southwest Block 23": ([4, 3, 3, 6, 3, 4, 4, 3, 3, 2], 35),
        "Southwest Block 24": ([7, 4, 5, 6, 5, 5, 3, 1, 5, 2], 43),
        "Northeast Block 7": ([5, 3, 2, 4, 6, 6, 2, 4, 4, 1], 37),
        "Northeast Block 8": ([4, 4, 4, 3, 2, 4, 4, 0, 1, 0], 26)}


# ------------------------------------------------------------------ Q9
def test_q9_well_0001_rules_broken_last_12_months(dom):
    c = dom.asset_compliance("WELL-0001", TimeWindow(last_n=12, unit="months"))
    breaches = {b["protocol"]: b for b in c["breaches"]}
    assert set(breaches) == {"SR-01", "SR-02", "SR-03", "RC-04"} and len(c["breaches"]) == 4
    assert "41" in breaches["SR-01"]["detail"] and "REG-0001-001" in breaches["SR-01"]["refs"][0]
    assert "7 h" in breaches["SR-02"]["detail"] and "MNT-0001-029" in breaches["SR-02"]["detail"]
    assert "2026-08-10" in breaches["SR-03"]["detail"] and "REG-0001-003" in breaches["SR-03"]["refs"][0]
    assert "2026-07-30" in breaches["RC-04"]["detail"] and "REG-0001-002" in breaches["RC-04"]["refs"][0]
    assert c["fines"]["total_usd"] == 5000 and c["fines"]["open_usd"] == 5000
    pend = {p["protocol"]: p for p in c["pending"]}
    assert set(pend) == {"RCA", "WS/Manual-4"}
    assert pend["RCA"]["refs"] == ["maintenance:MNT-0001-028", "maintenance:MNT-0001-029", "maintenance:MNT-0001-032"]
    assert "CTX-0001-003" in pend["WS/Manual-4"]["detail"]
    rc02 = [m for m in c["met"] if m["protocol"] == "RC-02" and m["title"].endswith("2026")]
    assert len(rc02) == 1 and "2026-03-07" in rc02[0]["detail"]   # yearly flowline test done 7 Mar 2026 -> met


# ------------------------------------------------------------------ Q10
def test_q10_sw24_uncontained_within_4h(dom):
    sp = dom.spills(ids(dom, SW24), TimeWindow(last_n=90, unit="days"))
    late = {}
    for r in sp["rows"]:
        if r["voided"]:
            continue
        for k in dom.spill_compliance(r)["checks"]:
            if k["protocol"] == "SR-02" and k["verdict"] == "breached":
                late[r["asset_id"]] = (r["inspection_id"], k["actual_hours"])
    assert late == {"WELL-0082": ("INS-0082-0198", 7.1), "WELL-0100": ("INS-0100-0196", 8.1)}


# ------------------------------------------------------------------ Q11 (numbers; conversational recall is tested in test_feedback_eval)
def test_q11_cost_this_year(dom):
    cost = {w: dom.maintenance([w], TimeWindow(calendar_year=2026))["total_cost_usd"] for w in ("WELL-0121", "WELL-0138", "WELL-0107")}
    assert cost == {"WELL-0121": 25794, "WELL-0138": 46942, "WELL-0107": 5176}


# ------------------------------------------------------------------ Q14 (base case)
def test_q14_rca_pump_jack_ne7_manual_rule(dom):
    rows = dom.rca_status(ids(dom, NE7), "pump_jack")["rows"]
    need = {r["asset_id"] for r in rows if r["rca_required"]}
    assert need == {"WELL-0108", "WELL-0121", "WELL-0138"}
    assert all(r["corrective_repairs"] == 3 and r["threshold_source"] == "document" for r in rows if r["rca_required"])
    two = {r["asset_id"]: r["well_type"] for r in rows if r["corrective_repairs"] == 2}
    assert two == {"WELL-0102": "T200", "WELL-0120": "T350"}


# ------------------------------------------------------------------ window arithmetic edge cases
@pytest.mark.parametrize("kw,start,end", [
    (dict(last_n=4, unit="months"), "2026-06-16", "2026-10-16"),
    (dict(last_n=12, unit="months"), "2025-10-16", "2026-10-16"),
    (dict(last_n=90, unit="days"), "2026-07-18", "2026-10-16"),
    (dict(last_n=13, unit="weeks"), "2026-07-17", "2026-10-16"),
    (dict(calendar_year=2025), "2025-01-01", "2025-12-31"),
    (dict(calendar_year=2026), "2026-01-01", "2026-10-16"),
])
def test_time_windows(dom, kw, start, end):
    s, e, _ = TimeWindow(**kw).resolve(dom.as_of)
    assert (s.isoformat(), e.isoformat()) == (start, end)


def test_time_window_rejects_mixed_styles():
    with pytest.raises(ValueError):
        TimeWindow(last_n=3, unit="days", calendar_year=2025)
    with pytest.raises(ValueError):
        TimeWindow(last_n=3)
