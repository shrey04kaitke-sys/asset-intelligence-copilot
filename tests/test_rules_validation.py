"""Rule validation at ingestion: implausible values are quarantined; disagreeing documents make a rule unavailable and visible.

Uses the small synthetic dataset from test_ingest (not the challenge data), so every case is constructed here.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from copilot.config import Settings
from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect, open_source
from copilot.domain import Domain
from copilot.ingest import ingest
from copilot.state import StateStore

from .test_ingest import count, jl, mini  # noqa: F401  (mini is a pytest fixture)


def domain(s, conn):
    return Domain(conn, StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION)))


def second_manual(root, text, name="T900-addendum.md"):
    (root / "fleet" / "well-types" / name).write_text(f'# Addendum\nApplies to all wells with `well_type = "T900"`\n## 7. Rule\n{text}\n', encoding="utf-8")


def issues(conn, code):
    return [dict(r) for r in conn.execute("SELECT * FROM ingest_issues WHERE code=?", (code,))]


def test_implausible_threshold_is_quarantined_and_reported(mini):
    s, root = mini
    (root / "fleet" / "well-types" / "T900.md").write_text('# T900\nApplies to all wells with `well_type = "T900"`\n## 3. Rule\nProducing when at least **500 °C above ambient** air.\n', encoding="utf-8")
    conn = open_source(s)
    ingest(s, conn)
    d = domain(s, conn)
    assert d.rule("production.min_delta_c", "T900") is None                      # not applied
    i = issues(conn, "rule_out_of_range")
    assert len(i) == 1 and "production.min_delta_c" in i[0]["message"] and "500" in i[0]["message"] and i[0]["severity"] == "error"
    assert d.status("WELL-0900")["producing"] is None                             # unknown, not computed from a nonsense rule


@pytest.mark.parametrize("text,key", [("Production alarm at or below **500 °C**.", "temp_alarm_c"), ("If a spill covers **more than 250 % of the pad area**, shut in.", "spill.shutin_pad_pct"),
                                      ("Three or more **corrective** events in the 12 months.", None)])
def test_other_out_of_range_values_are_rejected_in_range_ones_kept(mini, text, key):
    s, root = mini
    second_manual(root, text)
    conn = open_source(s)
    ingest(s, conn)
    d = domain(s, conn)
    if key:
        assert d.rule(key, "T900") is None and any(key in i["message"] for i in issues(conn, "rule_out_of_range"))
    else:
        assert d.rule("rca.min_corrective_events", "T900").value == 3.0 and issues(conn, "rule_out_of_range") == []     # a sane value passes validation


def test_two_documents_disagreeing_make_the_rule_unavailable_and_visible(mini):
    s, root = mini
    second_manual(root, "Producing when at least **18 °C above ambient** air.")           # the first manual says 12
    conn = open_source(s)
    ingest(s, conn)
    d = domain(s, conn)
    assert d.rule("production.min_delta_c", "T900") is None                      # ambiguity is never silently resolved
    c = d.rule_conflicts()
    assert len(c) == 1 and c[0]["rule_key"] == "production.min_delta_c" and {x["value"] for x in c[0]["sources"]} == {12.0, 18.0}
    assert {x["doc"] for x in c[0]["sources"]} == {"fleet/well-types/T900.md", "fleet/well-types/T900-addendum.md"}
    e = issues(conn, "conflicting_rule")
    assert len(e) == 1 and "12" in e[0]["message"] and "18" in e[0]["message"] and "T900-addendum.md" in e[0]["message"]
    assert d.status("WELL-0900")["producing"] is None
    assert any(r["rule_key"] == "production.min_delta_c" for r in [x for x in d.list_rules("T900")] ) is False         # not listed as an enforced rule


def test_agreeing_duplicates_are_not_a_conflict(mini):
    s, root = mini
    second_manual(root, "Producing when at least **12 °C above ambient** air (repeated for emphasis).")
    conn = open_source(s)
    ingest(s, conn)
    d = domain(s, conn)
    assert d.rule("production.min_delta_c", "T900").value == 12.0 and d.rule_conflicts() == [] and issues(conn, "conflicting_rule") == []


def test_removing_the_conflicting_document_restores_the_rule(mini):
    s, root = mini
    second_manual(root, "Producing when at least **18 °C above ambient** air.")
    conn = open_source(s)
    ingest(s, conn)
    assert domain(s, conn).rule("production.min_delta_c", "T900") is None
    (root / "fleet" / "well-types" / "T900-addendum.md").unlink()
    ingest(s, conn)
    d = domain(s, conn)
    assert d.rule("production.min_delta_c", "T900").value == 12.0 and d.rule_conflicts() == []


def test_inconsistent_spill_class_thresholds_are_reported(mini):
    s, root = mini
    (root / "fleet" / "sops" / "spill-response-sop.md").write_text(
        "# Spill SOP\n| Class | Estimated crude volume | Notes |\n|---|---|---|\n| Minor | below 10 bbl | x |\n| Moderate | 10 bbl to below 5 bbl | x |\n"
        "| Reportable | 5 bbl to below 25 bbl | x |\n| Major | 25 bbl or more | x |\n", encoding="utf-8")
    conn = open_source(s)
    ingest(s, conn)
    assert len(issues(conn, "inconsistent_spill_classes")) == 1


def test_rule_provenance_survives_in_the_rules_listing(mini):
    s, _ = mini
    conn = open_source(s)
    ingest(s, conn)
    r = next(x for x in domain(s, conn).list_rules("T900") if x["rule_key"] == "production.min_delta_c")
    assert r["value"] == 12.0 and r["source"] == "fleet/well-types/T900.md" and "12" in r["excerpt"] and r["origin"] == "document"


def test_an_old_source_database_is_rebuilt_not_corrupted(tmp_path):
    """The source DB is derived data: an older schema is recreated (and re-ingested), never half-migrated."""
    s = Settings(data_dir=tmp_path / "d", dataset_dir=tmp_path)
    (tmp_path / "d").mkdir()
    c = sqlite3.connect(s.source_db)
    c.execute("CREATE TABLE rules (rule_key TEXT, scope_kind TEXT, scope_value TEXT, value_num REAL, unit TEXT, source_doc TEXT, source_section TEXT, excerpt TEXT, PRIMARY KEY (rule_key, scope_kind, scope_value))")
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    conn = open_source(s)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    pk = [r[1] for r in conn.execute("PRAGMA table_info(rules)") if r[5]]
    assert "source_doc" in pk


# ---------------------------------------------------------------- persistent state survives a rebuild of the derived source database
def test_rebuilding_the_source_database_preserves_corrections_claims_and_memory(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    st = StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION))
    d = Domain(conn, st)
    from copilot.feedback import Feedback
    st_users = {"U-1": {"user_id": "U-1", "name": "A B", "role": "production_supervisor", "team_id": "T"}}
    d._users = st_users
    fb = Feedback(d)
    fb.correct_inspection(user_id="U-1", inspection_id="INS-0900-0002", field="wellhead_temp_c", new_value=40.0, reason="re-measured")
    fb.add_claim(user_id="U-1", asset_id="WELL-0900", text="planned stop", claim_type="planned_shutdown", valid_from="2026-03-20")
    st.remember(user_id="U-1", conversation_id="CONV-x", topic="t", question="q", entity_ids=["WELL-0900"], window=None, tool_name="get_downtime")
    assert d.eff_inspections(["WELL-0900"])[1]["producing"] is True
    conn.close()
    s.source_db.unlink()                                                                # the derived database is thrown away ...
    for ext in ("-wal", "-shm"):
        p = s.source_db.with_name(s.source_db.name + ext)
        p.exists() and p.unlink()
    conn2 = open_source(s)
    r = ingest(s, conn2)                                                                # ... and rebuilt from the untouched dataset
    assert r["files"]["added"] > 0 and r["files"]["unchanged"] == 0
    d2 = Domain(conn2, StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION)))
    d2._users = st_users
    assert d2.eff_inspections(["WELL-0900"])[1]["producing"] is True                    # the correction still applies
    assert len(d2.state.claims("WELL-0900", "U-1")) == 1 and len(d2.state.recall("U-1")) == 1
    assert d2.state.audit_trail("correction")[0]["actor_user_id"] == "U-1"


def test_corrections_whose_target_disappeared_are_flagged_not_silently_dropped(mini):
    s, root = mini
    conn = open_source(s)
    ingest(s, conn)
    st = StateStore(connect(s.data_dir / "st.db", STATE_DDL, STATE_SCHEMA_VERSION))
    d = Domain(conn, st)
    d._users = {"U-1": {"user_id": "U-1", "name": "A B", "role": "production_supervisor", "team_id": "T"}}
    from copilot.feedback import Feedback
    Feedback(d).correct_inspection(user_id="U-1", inspection_id="INS-0900-0002", field="wellhead_temp_c", new_value=40.0, reason="re-measured")
    jl(root / "assets" / "WELL-0900" / "inspections.jsonl", [dict(json.loads(l)) for l in open(root / "assets" / "WELL-0900" / "inspections.jsonl", encoding="utf-8") if '"INS-0900-0001"' in l])
    ingest(s, conn)                                                                     # INS-0900-0002 is gone from the data
    from copilot.feedback import Feedback as F2
    from copilot.tools import build_registry
    from copilot.views import Views
    k = Views(d, build_registry(), F2(d)).knowledge("U-1")
    assert [c["correction_id"] for c in k["corrections"] if c["orphaned"]] == ["COR-0001"] and len(k["corrections"]) == 1       # still listed, marked
