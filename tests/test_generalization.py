"""Generalisation: a second dataset in the documented format with different ids, types, areas, people and SOP numbers.

The dataset and its answer key come from scripts/make_variant_dataset.py, which computes the expected values with plain,
independent code from the records it writes. Nothing here reads the challenge dataset, and no expected value is taken from
the application. This checks that rules are *read from the documents* and nothing is tuned to WELL-0001..0200.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.config import ROOT, Settings
from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect, open_source
from copilot.domain import Domain
from copilot.feedback import Feedback
from copilot.ingest import ingest
from copilot.orchestrator import Orchestrator
from copilot.state import StateStore
from copilot.timewin import TimeWindow
from copilot.tools import ToolContext, build_registry

from .test_orchestrator import ScriptedProvider


def _generator():
    spec = importlib.util.spec_from_file_location("make_variant_dataset", ROOT / "scripts" / "make_variant_dataset.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def variant(tmp_path_factory):
    base = tmp_path_factory.mktemp("variant")
    expected = _generator().build(base / "dataset")
    s = Settings(dataset_dir=base / "dataset", data_dir=base / "data")
    conn = open_source(s)
    summary = ingest(s, conn)
    return {"settings": s, "conn": conn, "summary": summary, "exp": expected, "root": base / "dataset"}


@pytest.fixture
def dom(variant, tmp_path):
    return Domain(variant["conn"], StateStore(connect(tmp_path / "st.db", STATE_DDL, STATE_SCHEMA_VERSION)))


@pytest.fixture(scope="module")
def reg():
    return build_registry()


def tool(reg, dom, name, args, user="U-901"):
    return reg.call(name, args, ToolContext(dom, Feedback(dom), user_id=user, allow_writes=False))


# ---------------------------------------------------------------- ingestion
def test_variant_ingests_cleanly_with_the_documented_warnings(variant):
    sm, exp = variant["summary"], variant["exp"]
    assert sm["records"]["asset"]["accepted"] == 12 == exp["wells"] and sm["records"]["inspection"].get("rejected", 0) == 0
    total = sum(a["inspections"] for a in exp["assets"].values())
    assert variant["conn"].execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == total
    assert sm["as_of"] == exp["data_date"] == "2027-03-12"
    errs = [dict(r) for r in variant["conn"].execute("SELECT code, message FROM ingest_issues WHERE severity='error'")]
    assert errs == [], errs


def test_a_manual_the_parser_cannot_read_is_reported_not_silently_ignored(variant):
    w = [dict(r) for r in variant["conn"].execute("SELECT * FROM ingest_issues WHERE code='no_production_rule'")]
    assert len(w) == 1 and "Z9" in w[0]["message"] and "2 wells" in w[0]["message"] and w[0]["severity"] == "warning"


# ---------------------------------------------------------------- rules come from the documents
def test_rules_are_read_from_this_datasets_own_documents(dom):
    r = dom.rule
    assert r("production.min_delta_c", "H100").value == 10 and r("production.min_delta_c", "H400").value == 16 and r("production.min_delta_c", "Z9") is None
    assert (r("rca.min_corrective_events", "H400").value, r("rca.window_months", "H400").value) == (2, 6)                        # inherited from the H100 manual
    assert r("rca.min_corrective_events", "H400").source_doc.endswith("H100.md")
    assert (r("sr01.report_hours").value, r("sr01.major_report_hours").value, r("sr02.contain_hours").value, r("sr03.verify_days").value) == (12, 1, 3, 45)
    assert (r("spill.class.moderate_min_bbl").value, r("spill.class.reportable_min_bbl").value, r("spill.class.major_min_bbl").value) == (0.5, 3, 20)
    assert (r("rc01.max_inspection_gap_days").value, r("rc03.report_after_days").value, r("rc04.nov_response_days").value, r("downtime.report_after_days", "H100").value) == (10, 10, 21, 10)
    assert dom.rule_conflicts() == [] and dom.rule_rejections() == []


# ---------------------------------------------------------------- status, downtime, spills against the independent answer key
def test_production_status_matches_the_key_for_every_well(dom, variant):
    exp = variant["exp"]["assets"]
    for aid, a in exp.items():
        st = dom.status(aid)
        assert st["producing"] == a["producing_now"], aid
        assert st["latest"]["inspection_id"] == a["latest_inspection"]
    assert dom.status("PAD-A05")["delta_c"] == 14.0 and dom.status("PAD-A05")["producing"] is False       # producing under H100's rule (10), not under H400's (16)
    assert dom.status("PAD-A09")["producing"] is None and dom.status("PAD-A09")["latest"]["producing"] is None


def test_fleet_counts_match_the_key(dom, variant):
    f = variant["exp"]["fleet"]
    rows = dom.fleet_status()["rows"]
    assert (sum(1 for r in rows if r["producing"] is True), sum(1 for r in rows if r["producing"] is False), sum(1 for r in rows if r["producing"] is None)) == (f["producing"], f["not_producing"], f["unknown"])


def test_downtime_matches_the_key_including_the_hot_day_idle_week(dom, variant):
    ids = [a for a, v in variant["exp"]["assets"].items() if v["downtime_days_last_90d"] is not None]
    rows = {r["asset_id"]: r["downtime_days"] for r in dom.downtime(ids, TimeWindow(last_n=90, unit="days"))["rows"]}
    for aid in ids:
        assert rows[aid] == variant["exp"]["assets"][aid]["downtime_days_last_90d"], aid
    assert rows["PAD-A02"] >= 7                                                           # 52 C wellhead on a 44 C day is idle: only the difference matters
    assert rows["PAD-A04"] == 21 and rows["PAD-A01"] == 14


def test_spills_classes_and_corrections_match_the_key(dom, variant):
    for aid, a in variant["exp"]["assets"].items():
        sp = dom.spills([aid], TimeWindow(), include_voided=True)
        counted = [(r["inspection_id"], r["volume_bbl"], r["class"]) for r in sp["rows"] if not r["voided"]]
        assert counted == [(s["inspection_id"], s["volume_bbl"], s["class"]) for s in a["spills"]], aid
        assert sorted(r["inspection_id"] for r in sp["rows"] if r["voided"]) == a["corrected_detections"], aid
    assert dom.spill_class(3.2) == "reportable" and dom.spill_class(2.9) == "moderate" and dom.spill_class(21) == "major"   # this SOP's thresholds, not 5 / 25


def test_sr01_and_sr02_verdicts_use_this_datasets_deadlines(dom, variant):
    for aid, a in variant["exp"]["assets"].items():
        got, sr02 = {}, set()
        for s in dom.spills([aid], TimeWindow())["rows"]:
            for c in dom.spill_compliance(s)["checks"]:
                if c["protocol"] == "SR-01":
                    got[s["inspection_id"]] = c["verdict"]
                if c["protocol"] == "SR-02" and c["verdict"] == "breached":
                    sr02.add(s["inspection_id"])
        assert got == a["sr01"], aid
        assert sr02 == set(a["sr02_breached"]), aid
    assert variant["exp"]["assets"]["PAD-A06"]["sr01"] == {"INS-PAD-A06-0051": "no_report_on_record_overdue"}


def test_rca_and_costs_match_the_key(dom, variant):
    need = {r["asset_id"]: r["equipment"] for r in dom.rca_status(None)["rows"] if r["rca_required"]}
    assert need == {"PAD-A03": "pump_jack"} and variant["exp"]["assets"]["PAD-A03"]["rca_required"] == ["pump_jack"]
    for aid, a in variant["exp"]["assets"].items():
        assert dom.maintenance([aid], TimeWindow(calendar_year=2027))["total_cost_usd"] == a["repair_cost_ytd"], aid


def test_compliance_findings_for_gaps_downtime_and_young_wells(dom):
    c4 = dom.asset_compliance("PAD-A04", TimeWindow(last_n=12, unit="months"))
    assert any(b["protocol"] == "RC-01" and "21 days" in b["detail"] for b in c4["breaches"])                                    # two missed weeks > 10 days
    assert any(p["protocol"] == "RC-03" and "21 consecutive days" in p["title"] for p in c4["pending"])
    c11 = dom.asset_compliance("PAD-A11", TimeWindow(last_n=12, unit="months"))
    assert not any(b["protocol"] == "RC-02" for b in c11["breaches"])                                                            # a well drilled 8 weeks ago owes no earlier annual test
    c2 = dom.asset_compliance("PAD-A02", TimeWindow(last_n=12, unit="months"))
    assert {b["protocol"] for b in c2["breaches"]} >= {"SR-01", "SR-02"} and c2["fines"]["open_usd"] == 3000
    nov = next(p for p in c2["pending"] if p["protocol"] == "RC-04")                                                             # notice served 13 days before "today"; this SOP allows 21
    assert "by 2027-03-20" in nov["detail"] and "no response recorded" in nov["detail"]


# ---------------------------------------------------------------- nothing is tuned to the challenge ids
def test_informal_notes_are_linked_even_with_a_different_inspection_id_format(dom):
    r = dom.spills(["PAD-A02"], TimeWindow())["rows"][0]
    assert r["inspection_id"] == "INS-PAD-A02-0049" and [n["author_name"] for n in r["informal_notes"]] == ["Tomas Ruiz"]


def test_tools_work_on_the_new_ids_areas_and_users(dom, reg):
    e = tool(reg, dom, "get_asset_status", {"asset_id": "pad-a01"})
    assert e.ok and e.summary["producing"] is False and e.summary["reasons"] and "gearbox" in e.summary["reasons"][0]
    e = tool(reg, dom, "list_asset_status", {"scope": {"area": "Delta North"}, "state": "not_producing"})
    assert [r["asset_id"] for r in e.rows] == ["PAD-A01"] and e.summary["wells_in_scope"] == 4
    assert tool(reg, dom, "list_asset_status", {"scope": {"area": "HE-07"}, "state": "not_producing"}).rows[0]["asset_id"] == "PAD-A05"
    e = tool(reg, dom, "get_spill_heatmap", {"window": {"calendar_year": 2027}})
    assert [r["area"] for r in e.rows] == ["Delta North", "Harbor East", "Mesa West"]
    assert tool(reg, dom, "get_asset_status", {"asset_id": "WELL-0001"}).error.code == "unknown_asset"                           # the challenge ids simply do not exist here
    users = {u["user_id"] for u in dom.users().values()}
    assert users == {"U-901", "U-902", "U-903", "U-904", "U-905", "U-906"}


def test_corrections_and_rules_work_on_the_new_dataset(dom):
    fb = Feedback(dom)
    r = fb.set_rule_override(user_id="U-901", rule_key="rca.min_corrective_events", value=3, scope="well_type", scope_value="H100", reason="team policy")
    assert r["original"] == 2 and not dom.rca_status(None)["rows"][0]["rca_required"] or all(not x["rca_required"] for x in dom.rca_status(None)["rows"] if x["asset_id"] == "PAD-A03")
    assert dom.rule("rca.min_corrective_events", "H400").value == 2                                                              # H400 has its own inherited value; scope is H100 only
    c = fb.correct_inspection(user_id="U-901", asset_id="PAD-A05", on_date="2027-03-11", field="wellhead_temp_c", new_value=60.0, reason="re-measured")
    assert c["after"]["producing"] is True and dom.status("PAD-A05")["producing"] is True


def test_workspace_api_and_fleet_overview_work_on_the_variant(variant, tmp_path):
    st = StateStore(connect(tmp_path / "s.db", STATE_DDL, STATE_SCHEMA_VERSION))
    d = Domain(variant["conn"], st)
    o = Orchestrator(d, st, Feedback(d), build_registry(), ScriptedProvider([]))
    c = TestClient(create_app(o, web_dir=tmp_path))
    f = c.get("/fleet").json()
    tot = {t["key"]: t["value"] for t in f["totals"]}
    exp = variant["exp"]["fleet"]
    assert tot["wells"] == 12 and tot["producing"] == exp["producing"] and tot["not_producing"] == exp["not_producing"] and tot["unknown"] == exp["unknown"]
    assert [a["name"] for a in f["by_area"]] == ["Delta North", "Harbor East", "Mesa West"] and f["well_types"] == ["H100", "H400", "Z9"]
    assert {x["asset_id"] for x in c.get("/assets?status=unknown").json()["rows"]} == {"PAD-A09", "PAD-A10"}
    detail = c.get("/assets/PAD-A02?user_id=U-901").json()
    assert detail["asset"]["well_type"] == "H100" and detail["spill_totals"]["count"] == 1 and detail["fines"]["open_usd"] == 3000
    ev = c.get("/evidence", params={"ref": "inspection:INS-PAD-A02-0049"}).json()
    assert ev["source"]["file"] == "assets/PAD-A02/inspections.jsonl" and ev["related"]
    rule = c.get("/evidence", params={"ref": "rule:production.min_delta_c@fleet/well-types/H100.md"}).json()
    assert "10 °C above ambient" in rule["excerpt"] and rule["source"]["file"] == "fleet/well-types/H100.md"
    ex = c.get("/examples").json()
    assert ex and all("WELL-" not in q["question"] for q in ex) and any("PAD-A" in q["question"] for q in ex)               # example questions come from the loaded data
    assert c.get("/health").json()["wells"] == 12


def test_incremental_reingest_and_a_new_asset_on_the_variant(variant, tmp_path):
    s = Settings(dataset_dir=variant["root"], data_dir=tmp_path / "d2")
    conn = open_source(s)
    ingest(s, conn)
    again = ingest(s, conn)
    assert again["files"]["added"] == 0 and again["files"]["changed"] == 0 and again["files"]["unchanged"] > 50
    new = variant["root"] / "assets" / "PAD-Z99"
    new.mkdir()
    meta = json.loads((variant["root"] / "assets" / "PAD-A01" / "metadata.json").read_text(encoding="utf-8"))
    meta.update(asset_id="PAD-Z99", name="New pad", last_inspection={})
    (new / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    ins = json.loads(open(variant["root"] / "assets" / "PAD-A01" / "inspections.jsonl", encoding="utf-8").readline())
    ins.update(inspection_id="INS-PAD-Z99-0001", asset_id="PAD-Z99", ambient_temp_c=20.0, wellhead_temp_c=45.0)
    (new / "inspections.jsonl").write_text(json.dumps(ins) + "\n", encoding="utf-8")
    try:
        r = ingest(s, conn)
        assert r["files"]["added"] == 2 and conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 13
        d = Domain(conn, StateStore(connect(tmp_path / "s3.db", STATE_DDL, STATE_SCHEMA_VERSION)))
        assert d.status("PAD-Z99")["producing"] is True
    finally:
        import shutil
        shutil.rmtree(new)
