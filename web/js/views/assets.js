import { h, mount, chip, tonedChip, notice, fmt, skeleton, emptyState, errorState, announce } from "../dom.js";
import { api } from "../api.js";
import { state, link } from "../state.js";
import { onRef } from "../evidence.js";

const SORTS = [["id", "Well id"], ["delta", "Wellhead above air"], ["inspected", "Last inspected"], ["area", "Area"], ["status", "Status"]];
const CONCERN = { open_nov: "open notice of violation", rca: "root-cause analysis required", sr01: "late/missing spill report (SR-01)", sr02: "spill not contained in time (SR-02)", sr03: "overdue clean-up verification (SR-03)" };

export async function render(container, { params, isCurrent = () => true }) {
  const q = { area: params.area || "", well_type: params.well_type || "", status: params.status || "all", stale: params.stale || "", reason: params.reason || "", concern: params.concern || "", q: params.q || "",
    sort: params.sort || "id", desc: params.desc || "", offset: Number(params.offset || 0) };
  const go = (patch) => { const n = { ...q, offset: 0, ...patch }; location.hash = link("assets", Object.fromEntries(Object.entries(n).filter(([k, v]) => v && !(k === "status" && v === "all") && !(k === "sort" && v === "id") && !(k === "offset" && !v)))); };
  const body = h("div");
  mount(container, h("div", { class: "page-head" }, h("div", null, h("h2", null, "Assets"), h("p", { class: "muted", id: "as-sub" }, "Loading…"))), body);
  mount(body, skeleton(8));
  let overview, d;
  try {
    overview = await api.fleet({ period: "90d", user_id: state.userId });
    d = await api.assets({ area: q.area, well_type: q.well_type, status: q.status, stale: q.stale ? "true" : "", reason: q.reason, concern: q.concern, q: q.q, sort: q.sort, desc: q.desc ? "true" : "", limit: 25, offset: q.offset, user_id: state.userId });
  } catch (e) { if (isCurrent()) mount(body, errorState(e, () => render(container, { params, isCurrent }))); return; }
  if (!isCurrent()) return;
  document.getElementById("as-sub").textContent = `${d.total} well${d.total === 1 ? "" : "s"} match · data as of ${d.as_of}`;
  const sel = (id, label, opts, val, key) => h("div", { class: "field" }, h("label", { for: id }, label), h("select", { id, onchange: (e) => go({ [key]: e.target.value }) }, opts.map(([v, t]) => h("option", { value: v, selected: v === (val || "") }, t))));
  const search = h("input", { id: "f-q", type: "search", value: q.q, placeholder: "Well id or name", "aria-label": "Search wells", onkeydown: (e) => { if (e.key === "Enter") go({ q: e.target.value }); } });
  const filters = h("div", { class: "filters", role: "search", "aria-label": "Asset filters" },
    sel("f-area", "Area", [["", "All areas"], ...overview.areas.map((a) => [a.area_id, a.name])], q.area, "area"), sel("f-type", "Well type", [["", "All"], ...overview.well_types.map((t) => [t, t])], q.well_type, "well_type"),
    sel("f-status", "Status now", [["all", "Any"], ["producing", "Producing"], ["not_producing", "Not producing"], ["unknown", "No usable inspection"]], q.status, "status"),
    sel("f-sort", "Sort by", SORTS, q.sort, "sort"),
    h("div", { class: "field" }, h("label", { for: "f-q" }, "Search"), search),
    h("button", { type: "button", class: "btn", onclick: () => go({ q: search.value }) }, "Search"),
    h("button", { type: "button", class: "btn btn-quiet", onclick: () => (location.hash = "#/assets") }, "Clear filters"));
  const active = [];
  if (q.stale) active.push("recorded status out of date");
  if (q.reason === "none") active.push("no recorded reason");
  if (q.concern) active.push(CONCERN[q.concern] || q.concern);
  const rows = d.rows;
  const table = rows.length ? h("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": "Assets (scrollable)" }, h("table", null,
    h("caption", { class: "sr-only" }, "Assets matching the filters"),
    h("thead", null, h("tr", null, ["Well", "Area", "Type", "Status now", "Above air (°C)", "Needs (°C)", "Recorded status", "Last inspected", "Reason / note"].map((t, i) => h("th", { scope: "col", class: i === 4 || i === 5 ? "num" : "" }, t)))),
    h("tbody", null, rows.map((r) => h("tr", null,
      h("td", null, h("a", { href: link(`assets/${r.asset_id}`) }, r.asset_id), h("div", { class: "small muted" }, r.name)), h("td", null, r.area), h("td", null, r.well_type),
      h("td", null, chip(r.status)), h("td", { class: "num" }, fmt.num(r.delta_c)), h("td", { class: "num" }, fmt.num(r.threshold_c, 0)),
      h("td", null, r.recorded_status, r.recorded_status_stale ? [" ", tonedChip("warn", "out of date")] : null), h("td", null, fmt.date(r.last_inspected)),
      h("td", null, r.reason || "", r.inspector_observation ? h("div", { class: "small muted" }, `Inspector: "${r.inspector_observation}"`) : null))))))
    : emptyState("No wells match these filters", "Widen the filters, or clear them.", h("a", { class: "btn", href: "#/assets" }, "Clear filters"));
  const pager = d.total > d.limit ? h("div", { class: "row section" }, h("button", { type: "button", class: "btn", disabled: q.offset === 0, onclick: () => go({ offset: Math.max(0, q.offset - d.limit) }) }, "← Previous"),
    h("span", { class: "muted small", role: "status" }, `${q.offset + 1}–${Math.min(d.total, q.offset + d.limit)} of ${d.total}`),
    h("button", { type: "button", class: "btn", disabled: q.offset + d.limit >= d.total, onclick: () => go({ offset: q.offset + d.limit }) }, "Next →")) : null;
  mount(body, filters, active.length ? notice("info", "Also filtering by", [active.join(", ")], h("button", { type: "button", class: "link-btn", onclick: () => go({ stale: "", reason: "", concern: "" }) }, "Remove")) : null, table, pager);
  announce(`${d.total} wells listed`);
}
