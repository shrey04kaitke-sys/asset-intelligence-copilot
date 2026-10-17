import { h, mount, chip, tonedChip, kindChip, notice, fmt, skeleton, emptyState, errorState, refChip, announce } from "../dom.js";
import { api } from "../api.js";
import { state, link } from "../state.js";
import { onRef } from "../evidence.js";
import { renderVisual, dataTable } from "../charts.js";
import { correctionForm, claimForm, ruleForm, withdrawCorrection } from "../forms.js";

const TABS = [["overview", "Overview"], ["inspections", "Inspections"], ["maintenance", "Maintenance"], ["spills", "Spills & compliance"], ["regulatory", "Regulatory"], ["rules", "Rules"], ["timeline", "Timeline"], ["knowledge", "Notes & corrections"]];
const verdictTone = (v) => ({ met: "ok", breached: "bad", no_report_on_record_overdue: "bad", pending: "warn", unknown: "unknown", action_required: "warn" }[v] || "info");

export async function render(container, { parts, params, isCurrent = () => true }) {
  const id = parts[1];
  const body = h("div");
  mount(container, h("p", null, h("a", { href: "#/assets" }, "← All assets")), body);
  mount(body, skeleton(8));
  let d;
  const reload = () => render(container, { parts, params, isCurrent });
  try { d = await api.asset(id, state.userId); } catch (e) { if (isCurrent()) mount(body, errorState(e, reload)); return; }
  if (!isCurrent()) return;
  const a = d.asset, st = d.status;
  const card = st.visuals && st.visuals.card;
  const producing = st.summary ? st.summary.producing : null;
  const statusKind = producing === true ? "producing" : producing === false ? "not_producing" : "unknown";
  const tab = params.tab && TABS.some(([k]) => k === params.tab) ? params.tab : "overview";
  const panels = {};
  const tablist = h("div", { class: "subnav", role: "tablist", "aria-label": "Asset sections" });
  const show = (k) => {
    for (const [key] of TABS) { panels[key].hidden = key !== k; const b = tablist.querySelector(`[data-tab="${key}"]`); b.setAttribute("aria-selected", String(key === k)); b.tabIndex = key === k ? 0 : -1; }
    history.replaceState(null, "", link(`assets/${a.asset_id}`, k === "overview" ? {} : { tab: k }));
  };
  TABS.forEach(([k, t], i) => tablist.append(h("button", { type: "button", role: "tab", id: `tab-${k}`, "aria-controls": `panel-${k}`, dataset: { tab: k }, onclick: () => show(k),
    onkeydown: (e) => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { const n = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length][0]; show(n); tablist.querySelector(`[data-tab="${n}"]`).focus(); } } }, t)));

  // ---- overview
  const warnings = [...(st.warnings || [])];
  const ask = (q) => h("a", { class: "btn", href: link("investigate", { q }) }, "Ask about this well: " + q.slice(0, 48) + (q.length > 48 ? "…" : ""));
  panels.overview = h("div", null,
    card ? renderVisual(card, { onRef }) : notice("warn", "No status", ["This well has no usable inspection."]),
    warnings.length ? notice("warn", "What to be aware of", warnings) : null,
    d.compliance && d.compliance.ok ? h("div", { class: "grid kpis section" },
      h("div", { class: "kpi" }, h("div", { class: "kpi-value" }, d.compliance.summary.breaches), h("div", { class: "kpi-label" }, "Rule breaches, last 12 months")),
      h("div", { class: "kpi" }, h("div", { class: "kpi-value" }, d.compliance.summary.pending), h("div", { class: "kpi-label" }, "Open / pending duties")),
      h("div", { class: "kpi" }, h("div", { class: "kpi-value" }, fmt.usd(d.fines.open_usd)), h("div", { class: "kpi-label" }, "Open fines")),
      h("div", { class: "kpi" }, h("div", { class: "kpi-value" }, d.spill_totals.count), h("div", { class: "kpi-label" }, `Spills on record (${d.spill_totals.total_bbl} bbl)`), d.spill_totals.corrected_excluded ? h("div", { class: "kpi-note" }, `${d.spill_totals.corrected_excluded} corrected as not-oil, excluded`) : null)) : null,
    h("div", { class: "row section" }, ask(`Is ${a.asset_id} producing oil right now? Show me its status card.`), ask(`Which rules has ${a.asset_id} broken in the last 12 months, and what is still open?`)));

  // ---- inspections
  const readings = (d.series.rows || []).slice().reverse();
  panels.inspections = h("div", null, d.series.ok && d.series.visuals.line ? renderVisual(d.series.visuals.line, { onRef }) : errorState({ message: (d.series.error || {}).message || "No readings." }),
    d.series.ok ? h("div", { class: "section" }, h("h3", null, "Weekly readings (most recent first)"), dataTable({ title: "Weekly readings", columns: [{ key: "date", label: "Date" }, { key: "wellhead_temp_c", label: "Wellhead", unit: "°C" }, { key: "ambient_temp_c", label: "Ambient", unit: "°C" },
      { key: "delta_c", label: "Above air", unit: "°C" }, { key: "threshold_c", label: "Needs", unit: "°C" }, { key: "producing", label: "Producing" }, { key: "corrected", label: "Corrected" }],
      rows: readings.map((r) => ({ ...r, corrected: r.corrected ? "yes" : "", _ref: `inspection:${r.inspection_id}` })), onRef })) : null);

  // ---- maintenance
  panels.maintenance = h("div", null, h("p", null, `${d.maintenance.length} repairs on record, total ${fmt.usd(d.maintenance_total_cost_usd)}.`),
    d.maintenance.length ? dataTable({ title: "Repairs", columns: [{ key: "started", label: "Started" }, { key: "maintenance_id", label: "Repair" }, { key: "type", label: "Type" }, { key: "equipment", label: "Equipment" }, { key: "issue", label: "Issue" }, { key: "cost_usd", label: "Cost", unit: "USD" }, { key: "open", label: "Open" }, { key: "report", label: "Report" }],
      rows: d.maintenance.map((m) => ({ started: m.started_at.slice(0, 10), maintenance_id: m.maintenance_id, type: m.type, equipment: m.equipment, issue: m.issue, cost_usd: m.cost_usd, open: m.open ? "open" : "", report: m.report_ref ? "report" : "", _ref: m.ref })), onRef })
      : emptyState("No repairs recorded"));

  // ---- spills & compliance
  const spillRows = d.spills.map((s) => ({ date: s.date, volume: s.volume_bbl, class: s.class, status: s.voided ? `corrected: not a spill (${s.void_source})` : "counted", original: s.original_volume_bbl ?? "", repairs: (s.repairs || []).join(", "), regulator: (s.regulatory_events || []).join(", "),
    checks: (s.checks || []).map((c) => `${c.protocol}: ${c.verdict.replace(/_/g, " ")}`).join("; "), _ref: `inspection:${s.inspection_id}` }));
  const comp = d.compliance;
  panels.spills = h("div", null, h("h3", null, "Spills"), d.spills.length ? dataTable({ title: "Spills", columns: [{ key: "date", label: "Date" }, { key: "volume", label: "Volume", unit: "bbl" }, { key: "class", label: "Class" }, { key: "status", label: "Counted?" }, { key: "original", label: "Original estimate", unit: "bbl" }, { key: "repairs", label: "Repairs" }, { key: "regulator", label: "Regulator events" }, { key: "checks", label: "SOP checks" }], rows: spillRows, onRef }) : emptyState("No spills recorded"),
    h("div", { class: "section" }, h("h3", null, "Compliance findings, last 12 months"), h("p", { class: "small muted" }, "Judged from recorded evidence only; a missing record is reported as missing, never assumed compliant."),
      comp.ok ? dataTable({ title: "Compliance", columns: [{ key: "status", label: "Status" }, { key: "protocol", label: "Rule" }, { key: "item", label: "Item" }, { key: "detail", label: "Detail" }, { key: "date", label: "Date" }], rows: comp.rows.map((r) => ({ ...r, _ref: r._ref })), onRef }) : errorState({ message: (comp.error || {}).message })));

  // ---- regulatory
  panels.regulatory = h("div", null, h("p", null, `Fines on record: ${fmt.usd(d.fines.total_usd)} (${fmt.usd(d.fines.open_usd)} still open).`), d.regulatory.length ? dataTable({ title: "Regulator events", columns: [{ key: "date", label: "Date" }, { key: "event_id", label: "Event" }, { key: "event_type", label: "Type" }, { key: "protocol_id", label: "Protocol" }, { key: "fine_usd", label: "Fine", unit: "USD" }, { key: "status", label: "Status" }, { key: "hours_to_report", label: "Hours to report" }, { key: "detail", label: "Detail" }],
    rows: d.regulatory.map((r) => ({ ...r, _ref: r.ref })), onRef }) : emptyState("No regulator events"));

  // ---- rules
  panels.rules = h("div", null, h("p", { class: "muted small" }, `Rules in force for ${a.well_type} wells, each with the document and section it was read from. Select one to see the passage.`),
    dataTable({ title: "Rules", columns: [{ key: "rule_key", label: "Rule" }, { key: "value", label: "Value" }, { key: "unit", label: "Unit" }, { key: "applies_to", label: "Applies to" }, { key: "origin", label: "Origin" }, { key: "source", label: "Source" }, { key: "section", label: "Section" }], rows: d.rules.map((r) => ({ ...r, _ref: r.ref })), onRef }),
    h("div", { class: "section" }, h("button", { type: "button", class: "btn", onclick: () => ruleForm({ scope_value: a.asset_id }) }, "Propose a team rule…")));

  // ---- timeline
  panels.timeline = d.timeline.ok && d.timeline.visuals.timeline ? renderVisual(d.timeline.visuals.timeline, { onRef }) : emptyState("No events in the last 12 months");

  // ---- knowledge
  const corr = d.corrections;
  panels.knowledge = h("div", null,
    h("div", { class: "row" }, h("button", { type: "button", class: "btn btn-primary", onclick: () => correctionForm({ asset_id: a.asset_id }) }, "Record a correction…"), h("button", { type: "button", class: "btn", onclick: () => claimForm({ asset_id: a.asset_id }) }, "Add a note or claim…")),
    h("div", { class: "section" }, h("h3", null, "Shared knowledge"), h("p", { class: "small muted" }, "Dataset notes and shared claims are visible to everyone; claims marked private are visible only to their author."),
      d.notes.length || d.claims.length ? h("ul", { class: "plain" }, [...d.notes.map((n) => h("li", null, kindChip("note"), " ", `${fmt.date(n.created_at)} ${n.author}: ${n.text}`, " ", refChip(n.ref, onRef, "open"))),
        ...d.claims.map((c) => h("li", null, tonedChip(c.visibility === "private" ? "unknown" : "info", c.visibility === "private" ? "Private claim" : "Shared claim"), " ", chip(c.verification), ` ${fmt.date(c.created_at)} ${c.author}: ${c.text}`, " ", refChip(c.ref, onRef, "open")))]) : emptyState("No notes or claims for this well")),
    h("div", { class: "section" }, h("h3", null, "Corrections affecting this well"), corr.length ? h("ul", { class: "plain" }, corr.map((c) => h("li", null, chip(c.status), ` ${c.kind.replace(/_/g, " ")} by ${c.author} on ${fmt.date(c.created_at)}: ${c.field} ${JSON.stringify(c.original_value)} → ${JSON.stringify(c.new_value)} (${c.scope_kind}: ${c.scope_value})`, c.reason ? ` — ${c.reason}` : "", " ",
      refChip(c.ref, onRef, c.correction_id), c.status === "active" ? [" ", h("button", { type: "button", class: "link-btn", onclick: () => withdrawCorrection(c.correction_id) }, "Withdraw")] : null))) : emptyState("No corrections recorded for this well")),
    h("div", { class: "section" }, h("h3", null, "Your investigations mentioning this well"), d.investigations.length ? h("ul", { class: "plain" }, d.investigations.map((i) => h("li", null, `${fmt.dt(i.when)} — `, h("a", { href: link("investigate", { c: i.conversation_id }) }, i.question)))) : emptyState("None yet", state.userId ? "Questions you ask that involve this well will be listed here (private to you)." : "Choose who you are to see your own investigations.")));

  const header = h("div", { class: "page-head" }, h("div", null, h("h2", null, a.asset_id, " ", h("span", { class: "muted", style: null }, "· " + a.name)),
    h("p", { class: "muted" }, `${a.well_type} · ${a.area} · team ${a.team_id} · in charge ${a.in_charge} · installed ${a.installed_on} · ${a.lat !== null ? `${a.lat}, ${a.lon}` : "no coordinates"}`),
    h("p", null, chip(statusKind), " ", refChip(a.ref, onRef, "asset record"))));
  mount(body, header, tablist, ...TABS.map(([k]) => { panels[k].id = `panel-${k}`; panels[k].setAttribute("role", "tabpanel"); panels[k].setAttribute("aria-labelledby", `tab-${k}`); return panels[k]; }));
  show(tab);
  announce(`${a.asset_id} loaded`);
}
