// Evidence drawer: opens the REAL record behind a citation (fetched from the backend), never a made-up preview.
import { h, mount, tonedChip, kindChip, notice, fmt, refChip, skeleton, announce } from "./dom.js";
import { api } from "./api.js";
import { dataTable } from "./charts.js";
import { state } from "./state.js";

const CATEGORY = {
  source: ["info", "Source evidence"], calculated: ["ok", "Calculated value"], rule: ["info", "Rule from a manual / SOP"], correction: ["warn", "User correction"],
  claim: ["warn", "User claim"], note: ["unknown", "Human note"], document: ["unknown", "Document"],
};
const stack = [];
let opener = null;
let hooks = {};

export function configureEvidence(h2) { hooks = h2; }
const els = () => ({ drawer: document.getElementById("drawer"), body: document.getElementById("drawer-body"), title: document.getElementById("drawer-title"), back: document.getElementById("drawer-back"), scrim: document.getElementById("scrim") });

export function initEvidence() {
  const e = els();
  document.getElementById("drawer-close").addEventListener("click", closeEvidence);
  e.back.addEventListener("click", () => { stack.pop(); const prev = stack.pop(); if (prev) openEvidence(prev.ref, prev.local, true); else closeEvidence(); });
  e.scrim.addEventListener("click", closeEvidence);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !e.drawer.hidden && !document.querySelector("dialog[open]")) closeEvidence(); });
}

export function closeEvidence() {
  const e = els();
  e.drawer.hidden = true; e.scrim.hidden = true; e.drawer.setAttribute("aria-hidden", "true"); stack.length = 0;
  if (opener && document.contains(opener)) opener.focus();
  opener = null;
}

// `local` is the citation object the answer carried; it lets calculated values (which have no stored record) be explained honestly.
export async function openEvidence(ref, local, fromBack = false) {
  const e = els();
  if (e.drawer.hidden) opener = document.activeElement;
  if (!fromBack) stack.push({ ref, local });
  e.drawer.hidden = false; e.scrim.hidden = false; e.drawer.setAttribute("aria-hidden", "false");
  e.back.hidden = stack.length < 2;
  e.title.textContent = ref;
  mount(e.body, skeleton(5));
  e.title.focus();
  try {
    const ev = await api.evidence(ref, state.userId);
    render(ev, e);
  } catch (err) {
    if (local && ref.startsWith("computed:")) renderLocal(ref, local, e);        // calculated values have no stored record; explain them from the citation itself
    else mount(e.body, notice("bad", "This evidence could not be opened", [err.message || String(err)]), local ? renderLocalFields(local) : null);
  }
}

function renderLocalFields(c) {
  const f = c.fields || {};
  return h("dl", { class: "fields" }, Object.entries(f).flatMap(([k, v]) => [h("dt", null, k.replace(/_/g, " ")), h("dd", null, typeof v === "object" ? JSON.stringify(v) : String(v))]));
}

function renderLocal(ref, c, e) {
  e.title.textContent = c.title || ref;
  mount(e.body, h("div", { class: "row" }, tonedChip("ok", "Calculated value"), kindChip(c.type)),
    notice("info", "No stored record", ["This value was calculated by deterministic backend code from the records listed in the answer's sources. It is not a quoted document, so there is no source line to open."]),
    renderLocalFields(c));
}

function render(ev, e) {
  const [tone, label] = CATEGORY[ev.category] || ["unknown", ev.label || ev.category];
  e.title.textContent = ev.title;
  announce(`Opened evidence: ${ev.title}`);
  const body = [
    h("div", { class: "row" }, tonedChip(tone, ev.label || label), ev.timestamp ? h("span", { class: "muted small" }, fmt.dt(ev.timestamp)) : null),
    ev.source && ev.source.file ? h("p", { class: "source-line" }, "Source: ", ev.source.file, ev.source.line ? `, line ${ev.source.line}` : "") : null,
    ev.fields && ev.fields.length ? h("dl", { class: "fields" }, ev.fields.flatMap((f) => [h("dt", null, f.label), h("dd", null, String(f.value))])) : null,
    ev.excerpt ? h("div", null, h("h4", null, ev.category === "rule" ? "Passage in the manual" : "Text"), h("blockquote", { class: "excerpt", tabindex: 0 }, ev.excerpt)) : null,
  ];
  if (ev.corrections && ev.corrections.length) body.push(h("div", { class: "section" }, h("h4", null, "Corrections applied to this record (the original values are kept)"),
    h("ul", null, ev.corrections.map((c) => h("li", null, refChip(`correction:${c.correction_id}`, openEvidence), ` ${c.field}: ${c.original} → ${c.new} by ${c.author}${c.reason ? " — " + c.reason : ""}`)))));
  if (ev.extracted_facts && ev.extracted_facts.length) body.push(h("div", { class: "section" }, h("h4", null, "Facts extracted from the report text"),
    dataTable({ title: "Extracted facts", filterable: false, columns: [{ key: "fact_key", label: "Fact" }, { key: "protocol_id", label: "Protocol" }, { key: "value", label: "Value" }, { key: "excerpt", label: "Report wording" }],
      rows: ev.extracted_facts.map((f) => ({ ...f, value: f.value_num ?? f.value_text })) })));
  if (ev.related && ev.related.length) body.push(h("div", { class: "section" }, h("h4", null, "Related records"), h("div", { class: "related" }, ev.related.map((r) => refChip(r.ref, openEvidence, r.title)))));
  if (ev.audit && ev.audit.length) body.push(h("div", { class: "section" }, h("h4", null, "Audit history"), h("ul", null, ev.audit.map((a) => h("li", null, `${fmt.dt(a.at)} — ${a.actor}: ${a.action.replace(/_/g, " ")}`)))));
  const actions = [];
  if (ev.actions && ev.actions.correct && hooks.correct) actions.push(h("button", { type: "button", class: "btn", onclick: () => hooks.correct(ev.actions.correct) }, "Correct a value on this record…"));
  if (ev.actions && ev.actions.withdraw && hooks.withdraw) actions.push(h("button", { type: "button", class: "btn btn-danger", onclick: () => hooks.withdraw(ev.actions.withdraw.correction_id) }, "Withdraw this correction…"));
  if (actions.length) body.push(h("div", { class: "section row" }, actions));
  mount(e.body, body);
}

export { openEvidence as onRef };
