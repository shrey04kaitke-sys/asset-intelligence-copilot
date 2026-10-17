"""Ingestion behaviour on a small synthetic dataset (separate from the challenge data).

Covers validation/rejection, idempotent re-ingestion, in-place edits, removed files and the inferred "today".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from copilot.config import Settings
from copilot.db import open_source
from copilot.extract import parse_manual, parse_report, parse_sop
from copilot.ingest import ingest, parse_date, parse_ts


def jl(path: Path, rows: list, raw: list[str] | None = None):
    lines = [json.dumps(r) for r in rows] + (raw or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ins(n, ts, wh=40.0, amb=20.0, **kw):
    return {"inspection_id": f"INS-0900-{n:04d}", "asset_id": "WELL-0900", "inspected_at": ts, "inspector_user_id": "U-1", "method": "drone",
            "ambient_temp_c": amb, "wellhead_temp_c": wh, "spill_detected": False, "spill_area_pct": None, "spill_est_volume_bbl": None,
            "evidence_images": [], "notes": "", **kw}


@pytest.fixture
def mini(tmp_path):
    root = tmp_path / "ds"
    a = root / "assets" / "WELL-0900"
    (a / "maintenance-reports").mkdir(parents=True)
    (root / "fleet" / "well-types").mkdir(parents=True)
    (root / "fleet" / "sops").mkdir(parents=True)
    (root / "fleet" / "areas.json").write_text(json.dumps({"areas": [{"area_id": "X-1", "name": "Block X", "team_id": "T", "center": {"lat": 1, "lon": 2}}], "teams": []}))
    (root / "fleet" / "users.json").write_text(json.dumps({"users": [{"user_id": "U-1", "name": "A B", "role": "field_inspector", "team_id": "T"}]}))
    (root / "fleet" / "well-types" / "T900.md").write_text('# T900 manual\nApplies to all wells with `well_type = "T900"`\n## 3. Rule\nProducing when at least **12 °C above ambient** air.\n', encoding="utf-8")
    (a / "metadata.json").write_text(json.dumps({"asset_id": "WELL-0900", "name": "n", "asset_category": "oil_well", "well_type": "T900", "area_id": "X-1",
                                                  "team_id": "T", "location": {"lat": 1.5, "lon": 2.5}, "recorded_status": "active", "equipment": [],
                                                  "last_inspection": {"inspection_id": "INS-0900-0001", "inspected_at": "2026-03-01T00:00:00Z"}}))
    jl(a / "inspections.jsonl", [ins(1, "2026-03-01T08:00:00Z"), ins(2, "2026-03-08T08:00:00Z", wh=21.0)],
       raw=["{not json", json.dumps(ins(3, "not-a-date")), json.dumps({**ins(4, "2026-03-15T08:00:00Z"), "asset_id": "WELL-9999"}),
            json.dumps(ins(1, "2026-03-22T08:00:00Z")),                               # duplicate id, different content
            json.dumps({**ins(5, "2026-03-29T08:00:00Z"), "spill_detected": True, "spill_est_volume_bbl": -2.0})])
    (a / "maintenance.jsonl").write_text("")
    (a / "regulatory-events.jsonl").write_text("")
    (a / "context.jsonl").write_text("")
    return Settings(dataset_dir=root, data_dir=tmp_path / "data"), root


def count(conn, t):
    return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]


def test_rejects_bad_records_without_aborting(mini):
    s, _ = mini
    conn = open_source(s)
    out = ingest(s, conn)
    assert count(conn, "inspections") == 2
    codes = sorted(r[0] for r in conn.execute("SELECT code FROM ingest_issues WHERE severity='error'"))
    assert codes == ["bad_json", "duplicate_id", "missing_field", "negative_volume", "unknown_asset"]
    assert out["records"]["inspection"]["rejected"] == 5 and out["records"]["inspection"]["accepted"] == 2
    assert out["as_of"] == "2026-03-09"   # inferred: day after newest accepted inspection
    row = conn.execute("SELECT wellhead_temp_c FROM inspections WHERE inspection_id='INS-0900-0001'").fetchone()
    assert row[0] == 40.0                  # first definition kept, duplicate ignored


def test_reingest_is_idempotent_and_incremental(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    again = ingest(s, conn)
    assert again["files"]["unchanged"] > 0 and again["files"]["added"] == 0 and again["files"]["changed"] == 0
    assert count(conn, "inspections") == 2


def test_changed_file_replaces_its_rows_without_duplicates(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    p = root / "assets" / "WELL-0900" / "inspections.jsonl"
    jl(p, [ins(1, "2026-03-01T08:00:00Z", wh=45.0), ins(6, "2026-04-05T08:00:00Z")])
    r = ingest(s, conn)
    assert r["files"]["changed"] == 1
    got = {r[0]: r[1] for r in conn.execute("SELECT inspection_id, wellhead_temp_c FROM inspections")}
    assert got == {"INS-0900-0001": 45.0, "INS-0900-0006": 40.0}      # INS-0002 removed with the line, no dupes


def test_removed_file_removes_its_records(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    (root / "assets" / "WELL-0900" / "inspections.jsonl").unlink()
    r = ingest(s, conn)
    assert r["files"]["removed"] == 1 and count(conn, "inspections") == 0 and count(conn, "assets") == 1


def test_new_asset_folder_is_picked_up_without_code_changes(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    b = root / "assets" / "WELL-0901"
    b.mkdir()
    meta = json.loads((root / "assets" / "WELL-0900" / "metadata.json").read_text())
    meta.update(asset_id="WELL-0901", location={"lat": 999, "lon": 2})
    (b / "metadata.json").write_text(json.dumps(meta))
    jl(b / "inspections.jsonl", [{**ins(1, "2026-05-01T08:00:00Z"), "inspection_id": "INS-0901-0001", "asset_id": "WELL-0901"}])
    r = ingest(s, conn)
    assert count(conn, "assets") == 2 and r["files"]["added"] == 2
    assert conn.execute("SELECT lat FROM assets WHERE asset_id='WELL-0901'").fetchone()[0] is None
    assert any(i[0] == "bad_location" for i in conn.execute("SELECT code FROM ingest_issues"))


def test_missing_manual_is_reported_not_guessed(mini):
    s, root = mini
    (root / "fleet" / "well-types" / "T900.md").unlink()
    conn = open_source(s)
    ingest(s, conn)
    assert any(r[0] == "no_manual" for r in conn.execute("SELECT code FROM ingest_issues"))
    from copilot.domain import Domain
    from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect
    from copilot.state import StateStore
    d = Domain(conn, StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION)))
    assert d.rule("production.min_delta_c", "T900") is None
    assert d.status("WELL-0900")["producing"] is None      # unknown, not silently defaulted


def test_new_well_type_manual_gives_rules_with_no_code_changes(mini):
    s, _ = mini
    conn = open_source(s)
    ingest(s, conn)
    from copilot.domain import Domain
    from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect
    from copilot.state import StateStore
    d = Domain(conn, StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION)))
    assert d.rule("production.min_delta_c", "T900").value == 12.0
    ins_ = d.eff_inspections(["WELL-0900"])
    assert [(i["delta_c"], i["producing"]) for i in ins_] == [(20.0, True), (1.0, False)]


def test_timestamp_and_date_parsing():
    assert parse_ts("2026-10-15T09:45:00Z") == "2026-10-15T09:45:00Z"
    assert parse_ts("2026-10-15T11:45:00+02:00") == "2026-10-15T09:45:00Z"
    assert parse_ts("2026-10-15") == "2026-10-15T00:00:00Z"
    for bad in ("", "yesterday", None, 5, "2026-13-40"):
        assert parse_ts(bad) is None
    assert parse_date("2026-10-15") == "2026-10-15" and parse_date("15/10/2026") is None


def test_report_fact_extraction_uses_only_text_present():
    txt = ("# Maintenance Report — MNT-1\n## What happened\nThe release was contained about 6.4 hours after the inspection.\n"
           "## Compliance check\n| Protocol | Result | Detail |\n|---|---|---|\n| SR-02 | **Not met** | Contained about 6.4 hours (limit 4 hours) |\n"
           "| SR-04 | Met | Spill recorded (moderate class) |\n")
    f = {(x.fact_key, x.protocol_id): x for x in parse_report(txt)}
    assert f[("containment_hours", "")].value_num == 6.4
    assert f[("compliance", "SR-02")].value_text == "not met" and f[("stated_spill_class", "SR-04")].value_text == "moderate"
    assert parse_report("# Maintenance Report — MNT-2\nNothing relevant here.\n") == []


def test_rule_parsers_return_nothing_for_unrecognised_text():
    assert parse_sop("| **SR-01** | do something sensible |\n", "x") == []
    assert parse_manual("no declaration here", "x") == ([], [])
