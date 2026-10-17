"""Regression tests for defects found by the live evaluation against the real model (see docs/qa-report.md).

Each test names the live question that exposed the defect. They run against the real ingested data and real tools.
"""
from __future__ import annotations

import json

import pytest

from copilot.llm.base import LLMError
from copilot.orchestrator import AskRequest, Orchestrator, OrchestratorConfig
from copilot.tools import ToolContext, build_registry

from .conftest import JAKE, JESSICA, MICHAEL
from .test_orchestrator import CORR, CORR_MSG, ScriptedProvider, mk


@pytest.fixture(scope="module")
def reg():
    return build_registry()


def run(reg, dom, fb, name, args, user=MICHAEL):
    return reg.call(name, args, ToolContext(dom, fb, user_id=user, allow_writes=False))


# -------------------------------------------------------------- Q16b: "Is any downtime planned for WELL-0007?" (model never opened the notes tool)
def test_status_surfaces_planned_downtime_even_if_the_model_never_calls_the_notes_tool(reg, dom, fb):
    fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="WELL-0007 will be shut in from 17 Oct for a 10-day planned workover", claim_type="planned_shutdown", valid_from="2026-10-17", valid_to="2026-10-27")
    e = run(reg, dom, fb, "get_asset_status", {"asset_id": "WELL-0007"}, user=MICHAEL)
    planned = e.summary["planned_or_upcoming"]
    assert len(planned) == 1 and "Jake Morales" in planned[0] and "2026-10-17" in planned[0] and "planned workover" in planned[0]
    assert any("Planned or upcoming" in n and "claim:CLM-0001" in n for n in e.narrative)
    assert "claim:CLM-0001" in {x.ref for x in e.evidence}


def test_private_planned_claims_never_appear_in_another_users_status(reg, dom, fb):
    fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="private: pump to be pulled", claim_type="planned_shutdown", valid_from="2026-10-20", visibility="private")
    assert run(reg, dom, fb, "get_asset_status", {"asset_id": "WELL-0007"}, user=MICHAEL).summary["planned_or_upcoming"] == []
    assert len(run(reg, dom, fb, "get_asset_status", {"asset_id": "WELL-0007"}, user=JAKE).summary["planned_or_upcoming"]) == 1


def test_expired_plans_are_not_reported_as_upcoming(reg, dom, fb):
    fb.add_claim(user_id=JAKE, asset_id="WELL-0007", text="was shut in last month", claim_type="planned_shutdown", valid_from="2026-08-01", valid_to="2026-08-10")
    assert run(reg, dom, fb, "get_asset_status", {"asset_id": "WELL-0007"}).summary["planned_or_upcoming"] == []


def test_notes_tool_description_points_at_planned_downtime(reg):
    spec = next(s for s in reg.function_specs() if s["name"] == "get_asset_notes")
    assert "planned" in spec["description"].lower() and "downtime" in spec["description"].lower()


# -------------------------------------------------------------- Q12b: after a correction WELL-0164 silently vanished from the list
def test_list_status_explains_wells_excluded_because_of_a_correction(reg, dom, fb):
    fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=41.0, reason="faulty thermal gun")
    e = run(reg, dom, fb, "list_asset_status", {"scope": {"area": "Northeast Block 8"}, "state": "not_producing"})
    assert [r["asset_id"] for r in e.rows] == ["WELL-0170", "WELL-0172", "WELL-0175"]
    ex = e.summary["excluded_by_correction"]
    assert len(ex) == 1 and ex[0]["asset_id"] == "WELL-0164" and ex[0]["correction_id"] == "COR-0001" and ex[0]["author"] == "Michael Turner"
    assert any("WELL-0164" in n and "not listed" in n and "COR-0001" in n and "24" in n and "41" in n for n in e.narrative)
    assert any("WELL-0164" in w and "corrected" in w for w in e.warnings) and "correction:COR-0001" in {x.ref for x in e.evidence}


def test_no_exclusion_note_without_a_status_changing_correction(reg, dom, fb):
    e = run(reg, dom, fb, "list_asset_status", {"scope": {"area": "Northeast Block 8"}, "state": "not_producing"})
    assert e.summary["excluded_by_correction"] == []
    fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0164", on_date="2026-10-15", field="wellhead_temp_c", new_value=26.0, reason="small fix; still below 15 above air? 26-19.9=6.1")
    e = run(reg, dom, fb, "list_asset_status", {"scope": {"area": "Northeast Block 8"}, "state": "not_producing"})
    assert e.summary["excluded_by_correction"] == [] and "WELL-0164" in [r["asset_id"] for r in e.rows]       # still not producing: stays listed, not "excluded"


# -------------------------------------------------------------- Q15b: reportable spill listed without its SR-01 status
def test_spill_list_carries_the_sr01_deadline_status_for_reportable_spills(reg, dom, fb):
    fb.correct_inspection(user_id=MICHAEL, asset_id="WELL-0151", on_date="2026-09-24", field="spill_est_volume_bbl", new_value=5.4, reason="crew measured")
    e = run(reg, dom, fb, "get_spills", {"scope": {"area": "Northeast Block 8"}, "window": {"last_n": 90, "unit": "days"}, "min_class": "reportable"})
    rep = e.summary["reportable"]
    assert [(r["asset_id"], r["volume_bbl"], r["sr01"]) for r in rep] == [("WELL-0151", 5.4, "no_report_on_record_overdue")]
    assert any("WELL-0151" in n and "written notice" in n and "none is recorded" in n for n in e.narrative)
    assert any("reportable" in w.lower() and "WELL-0151" in w for w in e.warnings)


def test_spill_list_reports_the_met_and_breached_deadlines_from_the_data(reg, dom, fb):
    e = run(reg, dom, fb, "get_spills", {"scope": {"asset_ids": ["WELL-0001"]}, "window": {"last_n": 12, "unit": "months"}})
    rep = e.summary["reportable"]
    assert [(r["asset_id"], r["sr01"]) for r in rep] == [("WELL-0001", "breached")]
    assert any("41" in n and "24" in n for n in e.narrative)                              # filed after 41 h against the 24 h limit


def test_spills_without_any_reportable_spill_have_an_empty_list(reg, dom, fb):
    assert run(reg, dom, fb, "get_spills", {"scope": {"area": "Northeast Block 8"}, "window": {"last_n": 90, "unit": "days"}}).summary["reportable"] == []


# -------------------------------------------------------------- Q12a: the correction was saved, then the model call failed and the user was told "no answer"
def test_failure_after_a_write_reports_what_was_already_recorded(dom, fb, state, reg):
    prov = ScriptedProvider([mk(calls=[CORR]), LLMError("rate_limit", "the request rate or token rate limit was hit", retry_after_s=40.0)])
    o = Orchestrator(dom, state, fb, reg, prov, OrchestratorConfig())
    r = o.ask(AskRequest(user_id=MICHAEL, question=CORR_MSG))
    assert not r.ok and r.error.code == "rate_limited" and r.answer is None
    assert len(state.corrections()) == 1                                                  # the write really happened
    assert len(r.actions_taken) == 1 and "COR-0001" in r.actions_taken[0] and "INS-0164-0205" in r.actions_taken[0]
    assert "already recorded" in r.error.message and "COR-0001" in r.error.message and "do not repeat" in r.error.message.lower()
    assert r.error.retry_after_s == 40.0


def test_success_also_lists_the_actions_taken(dom, fb, state, reg):
    o = Orchestrator(dom, state, fb, reg, ScriptedProvider([mk(calls=[CORR]), mk(text="Recorded [correction:COR-0001].")]), OrchestratorConfig())
    r = o.ask(AskRequest(user_id=MICHAEL, question=CORR_MSG))
    assert r.ok and len(r.actions_taken) == 1 and "COR-0001" in r.actions_taken[0]


def test_read_only_failures_report_no_actions(dom, fb, state, reg):
    o = Orchestrator(dom, state, fb, reg, ScriptedProvider([LLMError("server", "boom")]), OrchestratorConfig())
    r = o.ask(AskRequest(user_id=MICHAEL, question="Is WELL-0001 producing oil?"))
    assert not r.ok and r.actions_taken == [] and "already recorded" not in r.error.message


# -------------------------------------------------------------- Q15a: the answer must say who corrected what
def test_system_prompt_requires_naming_the_person_and_values_of_a_correction(dom, fb, state, reg):
    prov = ScriptedProvider([mk(text="ok")])
    Orchestrator(dom, state, fb, reg, prov, OrchestratorConfig()).ask(AskRequest(user_id=MICHAEL, question="Is WELL-0164 producing oil?"))
    s = prov.calls[0]["system"]
    assert "who made it" in s and "original" in s and "planned or upcoming" in s.lower() and "get_asset_notes" in s
