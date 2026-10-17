import { h, mount, chip, tonedChip, kindChip, notice, fmt, skeleton, emptyState, errorState, refChip, announce } from "../dom.js";
import { api } from "../api.js";
import { state, link } from "../state.js";
import { onRef } from "../evidence.js";
import { dataTable } from "../charts.js";

const toHash = (d) => link(d.route, d.params);

function select(id, label, options, value, onchange) {
  return h("div", { class: "field" }, h("label", { for: id }, label), h("select", { id, onchange: (e) => onchange(e.target.value) }, options.map(([v, t]) => h("option", { value: v, selected: v === (value || "") }, t))));
}

export async function render(container, { params, isCurrent = () => true }) {
  const q = { area: params.area || "", well_type: params.well_type || "", period: params.period || "90d" };
  const body = h("div");
  const go = (patch) => { const n = { ...q, ...patch }; location.hash = link("overview", { area: n.area, well_type: n.well_type, period: n.period === "90d" ? "" : n.period }); };
  mount(container, h("div", { class: "page-head" }, h("div", null, h("h2", null, "Fleet overview"), h("p", { class: "muted", id: "ov-sub" }, "Loading…"))), body);
  mount(body, skeleton(6));
  let d;
  try { d = await api.fleet({ ...q, user_id: state.userId }); } catch (e) { if (isCurrent()) mount(body, errorState(e, () => render(container, { params, isCurrent }))); return; }
  if (!isCurrent()) return;
  document.getElementById("ov-sub").textContent = `Data as of ${d.as_of} (${d.basis}). ${d.note}`;
  const filters = h("div", { class: "filters", role: "group", "aria-label": "Overview filters" },
    select("f-area", "Area", [["", "All areas"], ...d.areas.map((a) => [a.area_id, a.name])], d.filters.area, (v) => go({ area: v })),
    select("f-type", "Well type", [["", "All types"], ...d.well_types.map((t) => [t, t])], d.filters.well_type, (v) => go({ well_type: v })),
    select("f-period", "Period for events & indicators", [["30d", "Last 30 days"], ["90d", "Last 90 days"], ["12m", "Last 12 months"], ["ytd", "Year to date"], ["all", "All history"]], d.period, (v) => go({ period: v })));

  const kpis = h("ul", { class: "grid kpis plain", "aria-label": "Key indicators" }, d.totals.map((t) => h("li", null, h("a", { class: "kpi", href: toHash(t.drill), "aria-label": `${t.label}: ${t.value}. Open the list.` },
    h("div", { class: "kpi-value" }, fmt.num(t.value, 0)), h("div", { class: "kpi-label" }, t.label)))));

  const total = d.status_distribution.reduce((a, b) => a + b.value, 0) || 1;
  const dist = h("section", { class: "panel", "aria-labelledby": "dist-h" }, h("h3", { id: "dist-h" }, "Status distribution"),
    d.status_distribution.map((s, i) => { const f = h("div", { class: `bar-fill ${i === 1 ? "dark" : i === 0 ? "okc" : ""}` }); f.style.width = `${(s.value / total) * 100}%`;
      return h("div", { class: "bar-row" }, h("span", null, s.label), h("div", { class: "bar-track", role: "img", "aria-label": `${s.label}: ${s.value} of ${total}` }, f), h("span", { class: "mono" }, `${s.value} (${Math.round((s.value / total) * 100)}%)`)); }),
    h("p", { class: "small muted" }, "Each well counts once, by its latest weekly inspection and its well-type rule."));

  const period = h("section", { class: "panel", "aria-labelledby": "per-h" }, h("h3", { id: "per-h" }, `Activity — ${d.period_label}`),
    h("dl", { class: "fields" }, d.period_stats.flatMap((p) => [h("dt", null, p.label), h("dd", null, h("strong", null, fmt.num(p.value, 0)), ` ${p.unit}`, h("div", { class: "small muted" }, p.note))])));

  const areaTable = dataTable({ title: "By area", filterable: false, columns: [{ key: "name", label: "Area" }, { key: "wells", label: "Wells" }, { key: "producing", label: "Producing" }, { key: "not_producing", label: "Not producing" }, { key: "spills", label: `Spills (${d.period_label})` }, { key: "downtime_days", label: "Downtime", unit: "days" }], rows: d.by_area });
  const typeTable = dataTable({ title: "By well type", filterable: false, columns: [{ key: "well_type", label: "Type" }, { key: "wells", label: "Wells" }, { key: "producing", label: "Producing" }, { key: "not_producing", label: "Not producing" }], rows: d.by_type });

  const comp = h("section", { class: "panel", "aria-labelledby": "comp-h" }, h("h3", { id: "comp-h" }, "Compliance concerns"), h("p", { class: "small muted" }, "From regulator events, repair history and the SOP deadlines; select one to list the wells."),
    h("ul", { class: "pill-list", style: null }, d.compliance.map((c) => h("li", null, h("a", { class: "kpi", href: toHash(c.drill) }, h("div", { class: "kpi-value" }, fmt.num(c.value, 0)), h("div", { class: "kpi-label" }, c.label), c.note ? h("div", { class: "kpi-note" }, c.note) : null)))));

  const attention = h("section", { class: "panel", "aria-labelledby": "att-h" }, h("h3", { id: "att-h" }, `Wells needing a look (${d.attention_total})`),
    d.attention.length ? h("ul", { style: null, class: "plain" }, d.attention.map((a) => h("li", { class: "row", style: null }, tonedChip(a.severity === "high" ? "bad" : a.severity === "medium" ? "warn" : "info", a.severity === "high" ? "High" : a.severity === "medium" ? "Medium" : "Info"), " ",
      h("a", { href: link(`assets/${a.asset_id}`) }, a.asset_id), h("span", { class: "muted small" }, ` ${a.area} — ${a.reasons.join("; ")} `), refChip(a.ref, onRef, "evidence")))) : emptyState("Nothing flagged", "No well in this selection is unexplained, stale or under an open notice."));

  const events = h("section", { class: "panel", "aria-labelledby": "ev-h" }, h("h3", { id: "ev-h" }, "Recent events (last 45 days)"),
    d.recent_events.length ? h("ol", { class: "timeline" }, d.recent_events.map((e) => h("li", null, h("time", { datetime: e.date }, e.date), tonedChip(e.kind === "spill" || e.kind === "regulatory" ? "bad" : e.kind === "correction" ? "warn" : "info", e.kind), " ",
      e.asset_id ? h("a", { href: link(`assets/${e.asset_id}`) }, e.asset_id) : null, " ", e.label, e.detail ? h("span", { class: "muted small" }, " — " + e.detail) : null, " ", refChip(e.ref, onRef, "open")))) : emptyState("No recent events"));

  mount(body, filters, kpis, h("div", { class: "grid cols-2 section" }, dist, period), h("section", { class: "panel section" }, h("h3", null, "By area"), areaTable), h("section", { class: "panel section" }, h("h3", null, "By well type"), typeTable),
    h("div", { class: "section" }, comp), h("div", { class: "grid cols-2 section" }, attention, events));
  announce(`Fleet overview loaded: ${d.totals[0].value} wells`);
}
