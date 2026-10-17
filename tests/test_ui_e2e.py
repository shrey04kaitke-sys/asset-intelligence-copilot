"""UI-to-backend tests in a real browser (Microsoft Edge/Chrome via Playwright).

REAL: the browser, the web UI, the FastAPI app, the domain/tool code, the ingested sample dataset, the SQLite state.
SCRIPTED: only the language model (a scripted provider), because model output is not deterministic. These tests therefore prove
the interface, the wiring and the backend behaviour; model quality is measured separately in scripts/run_live_eval.py.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="playwright is not installed (pip install playwright)")
import uvicorn  # noqa: E402

from copilot.api import create_app  # noqa: E402
from copilot.config import ROOT  # noqa: E402
from copilot.llm.base import LLMError  # noqa: E402
from copilot.orchestrator import Orchestrator, OrchestratorConfig  # noqa: E402
from copilot.tools import build_registry  # noqa: E402

from .test_orchestrator import ScriptedProvider, STATUS, DOWNTIME, mk  # noqa: E402

expect = pw.expect
pytestmark = pytest.mark.e2e
IDLE = "() => !document.querySelector('#main [aria-busy]')"


@pytest.fixture(scope="session")
def browser():
    with pw.sync_playwright() as p:
        b = None
        for channel in ("msedge", "chrome"):
            try:
                b = p.chromium.launch(channel=channel, headless=True)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("no Edge/Chrome available for Playwright (UI tests not run)")
        yield b
        b.close()


@pytest.fixture
def serve(dom, fb, state):
    """serve(script) -> base url of a real server (real app, real data) whose model is scripted."""
    servers = []

    def start(script=(), **cfg):
        o = Orchestrator(dom, state, fb, build_registry(), ScriptedProvider(list(script)), OrchestratorConfig(**cfg))
        server = uvicorn.Server(uvicorn.Config(create_app(o), host="127.0.0.1", port=0, log_level="warning"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(200):
            if server.started and server.servers and server.servers[0].sockets:
                break
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        servers.append((server, t))
        start.provider = o.provider
        return f"http://127.0.0.1:{port}"
    yield start
    for server, t in servers:
        server.should_exit = True
        t.join(timeout=5)


class Session:
    def __init__(self, browser, base, viewport=None):
        self.ctx = browser.new_context(viewport=viewport or {"width": 1366, "height": 900})
        self.page = self.ctx.new_page()
        self.base = base
        self.problems: list[str] = []
        self.page.on("console", lambda m: self.problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        self.page.on("requestfailed", lambda r: self.problems.append(f"requestfailed: {r.url}"))
        self.page.goto(base + "/")
        self.page.wait_for_selector("#user-picker option", state="attached")

    def go(self, hash_: str):
        self.page.goto(f"{self.base}/{hash_}")
        self.page.wait_for_selector("#main .notice, #main h2", state="attached")
        self.page.wait_for_function(IDLE)
        return self.page

    def login(self, name: str):
        value = self.page.evaluate("(n) => [...document.querySelectorAll('#user-picker option')].find(o => o.textContent.startsWith(n)).value", name)
        self.page.select_option("#user-picker", value=value)
        self.page.wait_for_function(IDLE)

    def api(self, path):
        r = self.page.request.get(self.base + path)
        assert r.ok, (path, r.status)
        return r.json()

    def close(self):
        self.ctx.close()


@pytest.fixture
def ui(browser, serve):
    sessions = []

    def make(script=(), viewport=None, **cfg):
        s = Session(browser, serve(script, **cfg), viewport)
        sessions.append(s)
        return s
    yield make
    for s in sessions:
        s.close()


def no_problems(s: Session):
    assert s.problems == [], s.problems


# ================================================================================ load, overview, drill-down
def test_app_loads_with_real_data_and_user_picker(ui):
    s = ui()
    p = s.page
    assert "Asset Intelligence Workspace" in p.title()
    assert "200 wells" in p.locator("#data-banner").inner_text() and "2026-10-16" in p.locator("#data-banner").inner_text() and "no live drone" in p.locator("#data-banner").inner_text()
    assert p.locator("#user-picker option").count() == 15 == len(s.api("/users"))
    assert p.locator("#user-picker").input_value() == "U-101"                         # first production supervisor, never an unchosen identity
    no_problems(s)


def test_overview_numbers_match_the_backend_and_drill_into_the_list(ui):
    s = ui()
    p = s.go("#/overview")
    api = s.api("/fleet")
    shown = {a.get_attribute("aria-label").split(":")[0]: a.locator(".kpi-value").inner_text() for a in p.locator("ul.kpis a.kpi").all()}
    expected = {t["label"]: f"{t['value']:,}" for t in api["totals"]}
    assert shown == expected and shown["Wells"] == "200" and shown["Not producing now"] == "15"
    p.get_by_role("link", name=re.compile("Not producing now: 15")).click()
    p.wait_for_function(IDLE)
    assert "status=not_producing" in p.url
    assert p.locator("#main tbody tr").count() == 15 == s.api("/assets?status=not_producing&limit=200")["total"]
    assert "15 wells match" in p.locator("#as-sub").inner_text()
    no_problems(s)


def test_overview_filters_and_empty_and_attention_lists(ui):
    s = ui()
    p = s.go("#/overview")
    p.select_option("#f-area", label="Northeast Block 8")
    p.wait_for_function(IDLE)
    assert "area=NE-08" in p.url and p.locator("ul.kpis a.kpi").first.locator(".kpi-value").inner_text() == "50"
    names = p.locator("#att-h").locator("xpath=..").inner_text()
    assert "WELL-0164" in names and "WELL-0175" in names and "no recorded reason" in names
    no_problems(s)


def test_assets_filter_shows_the_expected_four_wells_with_text_status(ui):
    s = ui()
    p = s.go("#/assets?area=NE-08&status=not_producing")
    rows = p.locator("#main tbody tr")
    assert [rows.nth(i).locator("td").first.locator("a").inner_text() for i in range(rows.count())] == ["WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"]
    body = p.locator("#main tbody").inner_text()
    assert body.count("Not producing") == 4 and "No recorded reason" in body and "MNT-0170-031" in body and 'Inspector: "Pump jack not moving."' in body    # status is text + shape, not colour alone
    no_problems(s)


def test_assets_empty_state_and_sorting_and_search(ui):
    s = ui()
    p = s.go("#/assets?q=ZZZ-NOTHING")
    assert "No wells match these filters" in p.locator("#main").inner_text() and p.get_by_role("link", name="Clear filters").count() >= 1
    p = s.go("#/assets?q=WELL-0164")
    assert p.locator("#main tbody tr").count() == 1
    p = s.go("#/assets?sort=delta&status=not_producing")
    deltas = [float(x) for x in p.locator("#main tbody tr td:nth-child(5)").all_inner_texts()]
    assert deltas == sorted(deltas) and deltas[0] < 1
    no_problems(s)


# ================================================================================ asset detail and evidence
def test_asset_page_status_card_reasons_and_unrecorded_shutdown(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001")
    card = p.locator(".status-card")
    t = card.inner_text()
    assert "Not producing oil" in t and "3.2 °C" in t and "Ryan Brooks" in t and "2026-10-15" in t and "CTX-0001-003" in t and "out of date" in t and 'Pump jack not moving.' in t
    assert "4\nRule breaches" in p.locator("#panel-overview").inner_text().replace("\r", "") and "USD 5,000" in p.locator("#panel-overview").inner_text()
    no_problems(s)


def test_citation_opens_the_real_source_record_with_file_and_line(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001")
    p.locator(".status-card .ref-chip").first.click()
    drawer = p.locator("#drawer")
    drawer.wait_for(state="visible")
    p.wait_for_function("() => document.getElementById('drawer-title').textContent === 'Inspection INS-0001-0203'")
    txt = drawer.inner_text()
    assert "Source record" in txt and "assets/WELL-0001/inspections.jsonl, line 203" in txt and "3.2 °C" in txt and "Ryan Brooks" in txt and "15.0 °C" in txt
    ev = s.api("/evidence?ref=inspection:INS-0001-0203")
    assert ev["source"]["line"] == 203 and any(f["label"] == "Wellhead above air" and f["value"] == "3.2 °C" for f in ev["fields"])
    p.keyboard.press("Escape")
    p.locator("#drawer").wait_for(state="hidden")
    assert p.evaluate("document.activeElement.classList.contains('ref-chip')")        # focus returns to what opened it
    no_problems(s)


def test_rule_evidence_shows_the_manual_passage(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001?tab=rules")
    p.locator("#panel-rules tbody tr", has_text="production.min_delta_c").first.locator("button").first.click()
    p.locator("#drawer").wait_for(state="visible")
    txt = p.locator("#drawer").inner_text()
    assert "Rule extracted from a manual/SOP" in txt and "fleet/well-types/T200.md" in txt and "15 °C above ambient" in txt and "Passage in the manual" in txt
    no_problems(s)


def test_spills_tab_marks_the_corrected_detection_and_shows_sop_checks(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001?tab=spills")
    body = p.locator("#panel-spills").inner_text()
    assert "corrected: not a spill (CTX-0001-001)" in body and "SR-01: breached" in body and "SR-02: breached" in body and "SR-03: breached" in body
    assert "RC-04" in body and "Breach" in body and "Pending / action" in body
    no_problems(s)


def test_line_chart_is_accessible_complete_and_has_a_data_table(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001?tab=inspections")
    svg = p.locator("#panel-inspections svg.chart").first
    assert svg.get_attribute("role") == "img" and "WELL-0001" in svg.get_attribute("aria-label")
    assert p.locator("#panel-inspections svg.chart polyline").count() >= 3
    assert "Not producing (4)" in p.locator("#panel-inspections .legend").inner_text()
    p.locator("#panel-inspections summary", has_text="values as a table").click()
    assert p.locator("#panel-inspections details table tbody tr").count() >= 20
    series = s.api("/assets/WELL-0001")["series"]["rows"]
    assert len(series) >= 50 and p.locator("#panel-inspections table").last.locator("tbody tr").count() >= 25
    no_problems(s)


def test_unknown_asset_shows_a_recoverable_error(ui):
    s = ui()
    p = s.go("#/assets/WELL-9999")
    t = p.locator("#main").inner_text()
    assert "Could not load this" in t and "not found" in t and p.get_by_role("button", name="Try again").count() == 1


# ================================================================================ investigate (scripted model)
ANSWER = "WELL-0001 is **not producing oil**: the wellhead was 3.2 °C above ambient, below the 15 °C minimum [inspection:INS-0001-0203]. A planned shut-in explains it [note:CTX-0001-003].\n[visual: card]"


def ask(p, text):
    p.fill("#q", text)
    p.press("#q", "Enter")


def test_ask_shows_answer_citations_card_sources_and_trace(ui):
    s = ui([mk(calls=[STATUS]), mk(text=ANSWER)])
    p = s.go("#/investigate")
    assert p.locator(".examples button").count() >= 6
    ask(p, "Is WELL-0001 producing oil right now? Show me its status card.")
    p.locator(".msg.assistant").wait_for()
    msg = p.locator(".msg.assistant")
    assert "not producing oil" in msg.inner_text() and "[visual" not in msg.inner_text()
    chips = msg.locator(".answer .ref-chip")
    assert chips.count() == 2 and chips.nth(0).get_attribute("data-ref") == "inspection:INS-0001-0203"
    assert msg.locator(".status-card").count() == 1 and "Ryan Brooks" in msg.locator(".status-card").inner_text()
    assert "Sources disagree or are out of date" in msg.inner_text() and "Cited in the answer (2)" in msg.inner_text()
    chips.nth(1).click()
    p.locator("#drawer").wait_for(state="visible")
    p.wait_for_function("() => document.getElementById('drawer-title').textContent === 'Note CTX-0001-003'")
    assert "Well shut in from 7 Oct" in p.locator("#drawer").inner_text() and "assets/WELL-0001/context.jsonl" in p.locator("#drawer").inner_text()
    p.keyboard.press("Escape")
    msg.locator("summary", has_text="How this answer was produced").click()
    assert "get_asset_status" in msg.locator("details.trace").inner_text() and "Reasoning text is never shown" in msg.locator("details.trace").inner_text()
    convs = s.api("/conversations?user_id=U-101")
    assert len(convs) == 1
    no_problems(s)


def test_invented_citations_are_not_clickable_and_are_reported(ui):
    s = ui([mk(calls=[STATUS]), mk(text="Not producing [inspection:INS-0001-0203] and also [inspection:INS-9999-0001] and [get_asset_status: summary].")])
    p = s.go("#/investigate")
    ask(p, "Is WELL-0001 producing oil?")
    p.locator(".msg.assistant").wait_for()
    msg = p.locator(".msg.assistant")
    assert msg.locator(".answer .ref-chip").count() == 1 and "INS-9999" not in msg.inner_text() and "2 citation(s) the model gave did not match" in msg.inner_text()
    no_problems(s)


def test_provider_failure_is_shown_with_diagnostics_and_a_retry(ui):
    s = ui([LLMError("rate_limit", "the request rate or token rate limit was hit", retry_after_s=63.0), mk(calls=[STATUS]), mk(text=ANSWER)])
    p = s.go("#/investigate")
    ask(p, "Is WELL-0001 producing oil right now?")
    p.locator(".msg.assistant").wait_for()
    t = p.locator(".msg.assistant").inner_text()
    assert "No answer was produced" in t and "rate limit" in t and "code: rate_limited" in t and "Suggested wait: about 63" in t and "rate limiting requests" in t
    assert p.locator(".msg.assistant .answer").count() == 0                                         # no fabricated fallback answer
    p.get_by_role("button", name="Try again").click()
    p.locator(".msg.assistant .answer").wait_for()
    assert "not producing oil" in p.locator(".msg.assistant .answer").inner_text()
    assert s.problems == ["console.error: Failed to load resource: the server responded with a status of 429 (Too Many Requests)"]     # the browser logs the (expected) 429 the UI handled


def test_bar_chart_and_table_from_real_downtime_data(ui):
    s = ui([mk(calls=[DOWNTIME]), mk(text="Top three [inspection:INS-0121-0001]\n[visual: bar]")])
    p = s.go("#/investigate")
    ask(p, "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart.")
    p.locator(".msg.assistant .bar-row").first.wait_for()
    rows = p.locator(".msg.assistant .bar-row")
    assert rows.count() == 3 and "WELL-0121" in rows.first.inner_text() and "49" in rows.first.inner_text()
    p.locator(".msg.assistant summary", has_text="values as a table").click()
    assert "WELL-0107" in p.locator(".msg.assistant details table").inner_text()
    no_problems(s)


def test_map_and_heatmap_render_real_values(ui):
    s = ui([mk(calls=[("get_spills", {"scope": {"area": "Northeast Block 8"}, "window": {"last_n": 90, "unit": "days"}})]), mk(text="Four spills.\n[visual: map]"),
            mk(calls=[("get_spill_heatmap", {"window": {"calendar_year": 2026}})]), mk(text="By area.\n[visual: heatmap]")])
    p = s.go("#/investigate")
    ask(p, "Show on a map every well in Northeast Block 8 with a real spill in the last 90 days.")
    p.locator(".msg.assistant svg.chart").wait_for()
    pts = p.locator(".msg.assistant svg.chart .pt")
    assert pts.count() == 4 and {pts.nth(i).get_attribute("aria-label").split(":")[0] for i in range(4)} == {"WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"}
    assert "schematic, no basemap" in p.locator(".msg.assistant .legend").inner_text()
    ask(p, "Show a heatmap of 2026 spills by geographic area and month.")
    p.locator(".msg.assistant table.heat").wait_for()
    heat = p.locator("table.heat").inner_text()
    assert "Southwest Block 24" in heat and "43" in heat and "2026-10" in heat
    no_problems(s)


# ================================================================================ corrections, claims, rules, authorization
def test_correction_flow_confirms_records_shows_impact_and_changes_the_status(ui):
    s = ui()
    s.login("Michael Turner")
    p = s.go("#/assets/WELL-0164?tab=knowledge")
    p.get_by_role("button", name=re.compile("Record a correction")).click()
    dlg = p.locator("dialog[open]")
    dlg.get_by_label("…and inspection date").fill("2026-10-15")
    dlg.get_by_label("Correct value").fill("41.0")
    dlg.get_by_label("Reason (required)").fill("handheld thermal gun faulty; crew re-measured 41.0")
    dlg.get_by_role("button", name=re.compile("Review and record")).click()
    assert "Record this correction?" in dlg.inner_text() and "original value is kept" in dlg.inner_text()
    dlg.get_by_role("button", name="Record correction").click()
    dlg.get_by_text("Recorded COR-0001").wait_for()
    t = dlg.inner_text()
    assert re.search(r"INS-0164-0205: wellhead_temp_c 24(\.0)? → 41", t) and "Original kept" in t and "not producing to producing" in t and "Pump jack not moving." in t and "Needs your confirmation" in t
    dlg.get_by_role("button", name="Close").click()
    expect(p.locator("#panel-knowledge")).to_contain_text("COR-0001")
    expect(p.locator("#panel-knowledge")).to_contain_text("Michael Turner")
    p = s.go("#/assets/WELL-0164")
    assert "Corrected reading" in p.locator(".status-card").inner_text() and p.locator(".status-card dl").inner_text().count("Yes") >= 1
    c = s.api("/corrections?asset_id=WELL-0164")
    assert c[0]["original_value"] == 24.0 and c[0]["new_value"] == 41.0 and c[0]["author"] == "Michael Turner" and c[0]["scope_value"] == "INS-0164-0205"
    p = s.go("#/assets?area=NE-08&status=not_producing")
    assert p.locator("#main tbody tr").count() == 3                                                    # Q2 again: WELL-0164 is gone
    no_problems(s)


def test_correction_is_visible_to_another_user_and_attributed(ui):
    s = ui()
    s.login("Michael Turner")
    s.page.request.post(s.base + "/feedback/correction", data={"user_id": "U-201", "asset_id": "WELL-0164", "date": "2026-10-15", "field": "wellhead_temp_c", "new_value": 41.0, "reason": "re-measured", "confirmed": True})
    s.login("Jessica Reed")
    p = s.go("#/assets/WELL-0164")
    t = p.locator(".status-card").inner_text()
    assert "Corrected reading" in t and "Disagreeing inspector note" in t
    assert "Pump jack not moving." in p.locator("#panel-overview").inner_text()                      # shown in the warnings beside the card
    p.locator(".status-card .ref-chip").first.click()
    p.locator("#drawer").wait_for(state="visible")
    assert "Michael Turner" in p.locator("#drawer").inner_text() and "24.0" in p.locator("#drawer").inner_text()
    no_problems(s)


def test_unconfirmed_write_is_refused_by_the_backend_and_ui_shows_authorization_errors(ui):
    s = ui()
    r = s.page.request.post(s.base + "/feedback/rule", data={"user_id": "U-201", "rule_key": "rca.min_corrective_events", "value": 2, "scope": "well_type", "scope_value": "T350", "reason": "policy", "confirmed": False})
    assert r.status == 400 and "confirmation" in r.json()["detail"]
    s.login("Tyler Hughes")
    p = s.go("#/knowledge")
    p.get_by_role("button", name=re.compile("Propose a team rule")).click()
    dlg = p.locator("dialog[open]")
    dlg.get_by_label("New value").fill("2")
    dlg.get_by_label("Well type or well id").fill("T350")
    dlg.get_by_label("Reason (required)").fill("team policy")
    dlg.get_by_role("button", name=re.compile("Review and apply")).click()
    dlg.get_by_role("button", name="Apply rule").click()
    dlg.get_by_text("Not permitted for your role").wait_for()
    assert "whole well type" in dlg.inner_text()
    assert s.api("/knowledge")["corrections"] == []                                                   # nothing was recorded
    assert s.problems == ["console.error: Failed to load resource: the server responded with a status of 403 (Forbidden)"]


def test_supervisor_rule_override_is_applied_at_t350_only_and_audited(ui):
    s = ui()
    s.login("Michael Turner")
    p = s.go("#/knowledge")
    p.get_by_role("button", name=re.compile("Propose a team rule")).click()
    dlg = p.locator("dialog[open]")
    dlg.get_by_label("New value").fill("2")
    dlg.get_by_label("Well type or well id").fill("T350")
    dlg.get_by_label("Reason (required)").fill("team wants RCA after 2 repairs")
    dlg.get_by_role("button", name=re.compile("Review and apply")).click()
    assert "every T350 well" in dlg.inner_text() and "not the manual" in dlg.inner_text()
    dlg.get_by_role("button", name="Apply rule").click()
    dlg.get_by_text(re.compile("Recorded COR-0001")).wait_for()
    dlg.get_by_role("button", name="Close").click()
    expect(p.locator("#kp-corrections")).to_contain_text("rule override")
    expect(p.locator("#kp-corrections")).to_contain_text("well_type: T350")
    p.get_by_role("tab", name="Audit trail").click()
    expect(p.locator("#kp-audit")).to_contain_text("correction created")
    expect(p.locator("#kp-audit")).to_contain_text("Michael Turner")
    res = s.api("/assets/WELL-0120")["rules"]
    assert any(r["rule_key"] == "rca.min_corrective_events" and r["value"] == 2 and r["origin"] == "user_override" for r in res)
    assert any(r["rule_key"] == "rca.min_corrective_events" and r["value"] == 3 and r["origin"] == "document" for r in s.api("/assets/WELL-0102")["rules"])
    no_problems(s)


def test_private_claims_are_private_in_the_ui(ui):
    s = ui()
    s.page.request.post(s.base + "/feedback/claim", data={"user_id": "U-102", "asset_id": "WELL-0007", "text": "my private reminder: check pump", "claim_type": "observation", "visibility": "private"})
    s.page.request.post(s.base + "/feedback/claim", data={"user_id": "U-102", "asset_id": "WELL-0007", "text": "WELL-0007 shut in from 17 Oct, planned workover", "claim_type": "planned_shutdown", "valid_from": "2026-10-17", "visibility": "shared"})
    s.login("Michael Turner")
    t = s.go("#/assets/WELL-0007?tab=knowledge").locator("#panel-knowledge").inner_text()
    assert "planned workover" in t and "Shared claim" in t and "private reminder" not in t
    s.login("Jake Morales")
    t = s.go("#/assets/WELL-0007?tab=knowledge").locator("#panel-knowledge").inner_text()
    assert "private reminder" in t and "Private claim" in t
    assert r"private reminder" not in str(s.api("/audit?user_id=U-201&limit=200")) and "private reminder" not in str(s.api("/knowledge?user_id=U-201"))
    r = s.page.request.get(s.base + "/evidence?ref=claim:CLM-0001&user_id=U-201")
    assert r.status == 404                                                                             # someone else's private claim looks like a missing one
    no_problems(s)


def test_claim_form_shows_the_backend_verdict_and_conflicts(ui):
    s = ui()
    s.login("Michael Turner")
    s.page.request.post(s.base + "/feedback/claim", data={"user_id": "U-201", "asset_id": "WELL-0175", "text": "shut in for planned work since 14 Oct", "claim_type": "planned_shutdown", "valid_from": "2026-10-14"})
    s.login("Tyler Hughes")
    p = s.go("#/assets/WELL-0175?tab=knowledge")
    p.get_by_role("button", name=re.compile("Add a note or claim")).click()
    dlg = p.locator("dialog[open]")
    dlg.get_by_label("What are you recording?").select_option("status_producing")
    dlg.get_by_label("Statement").fill("WELL-0175 is running fine")
    dlg.get_by_role("button", name="Record").click()
    dlg.get_by_text(re.compile("Recorded CLM-0002")).wait_for()
    t = dlg.inner_text()
    assert "contradicted" in t and "0.4 C above ambient" in t and "Conflicts with other statements" in t and "CLM-0001" in t and "Marked unconfirmed" in t
    no_problems(s)


# ================================================================================ responsive, accessibility, safety
@pytest.mark.parametrize("hash_", ["#/overview", "#/assets", "#/assets/WELL-0001", "#/assets/WELL-0001?tab=inspections", "#/assets/WELL-0001?tab=spills", "#/investigate", "#/knowledge"])
def test_no_horizontal_page_overflow_on_a_phone(ui, hash_):
    s = ui(viewport={"width": 390, "height": 844})
    p = s.go(hash_)
    p.wait_for_timeout(200)
    assert p.evaluate("document.documentElement.scrollWidth") <= 392, hash_
    no_problems(s)


def test_keyboard_and_semantics_basics(ui):
    s = ui()
    p = s.page                                                                                        # a fresh page load: no programmatic focus move yet
    p.wait_for_function(IDLE)
    p.keyboard.press("Tab")
    assert p.evaluate("document.activeElement.className") == "skip-link"
    p = s.go("#/assets")
    assert p.locator("main#main").count() == 1 and p.locator("nav[aria-label=Primary]").count() == 1 and p.locator("header[role=banner]").count() == 1
    assert p.locator("nav a[aria-current=page]").inner_text() == "Assets"
    unlabeled = p.evaluate("""() => [...document.querySelectorAll('input, select, textarea')].filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby')).map(e => e.id || e.name || e.tagName)""")
    assert unlabeled == []
    s.go("#/investigate")
    assert p.evaluate("document.querySelector('#q').getAttribute('aria-label')") == "Your question"
    # every tab in a tablist is reachable by arrow keys
    s.go("#/assets/WELL-0001")
    p.locator("#tab-overview").focus()
    p.keyboard.press("ArrowRight")
    assert p.evaluate("document.activeElement.id") == "tab-inspections" and p.locator("#panel-inspections").is_visible()
    no_problems(s)


def test_drawer_traps_nothing_but_closes_with_escape_and_button(ui):
    s = ui()
    p = s.go("#/assets/WELL-0001")
    p.locator(".status-card .ref-chip").first.click()
    p.locator("#drawer").wait_for(state="visible")
    assert p.evaluate("document.activeElement.id") == "drawer-title"
    p.get_by_role("button", name="Close evidence panel").click()
    p.locator("#drawer").wait_for(state="hidden")


def test_stale_slow_renders_never_overwrite_the_current_page(ui):
    """Regression: a slow earlier view finishing after navigation used to replace the page with an error."""
    s = ui()
    p = s.page
    p.route("**/fleet*", lambda route: (time.sleep(1.2), route.continue_()))
    p.goto(s.base + "/#/overview")
    p.goto(s.base + "/#/assets?status=not_producing")
    p.wait_for_selector("#main tbody tr")
    p.wait_for_timeout(2200)
    assert "Could not load" not in p.locator("#main").inner_text() and "wells match" in p.locator("#as-sub").inner_text()


def test_the_ui_never_uses_innerhtml_and_has_no_external_resources():
    web = ROOT / "web"
    for f in web.rglob("*.js"):
        src = re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S)
        src = re.sub(r"(?m)^\s*//.*$|(?<=\s)//\s.*$", "", src)                     # comments may mention the words; code may not use them
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert bad not in src, f"{f.name} uses {bad}"
    for f in list(web.rglob("*.js")) + list(web.rglob("*.css")) + list(web.rglob("*.html")):
        assert not re.search(r"https?://(?!www\.w3\.org)", f.read_text(encoding="utf-8")), f"{f.name} references an external URL"


def test_security_headers_and_csp_are_sent(ui):
    s = ui()
    r = s.page.request.get(s.base + "/")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
