import { h, mount, chip, tonedChip, kindChip, notice, fmt, skeleton, emptyState, errorState, refChip, announce } from "../dom.js";
import { api } from "../api.js";
import { state, link, currentUser } from "../state.js";
import { onRef } from "../evidence.js";
import { dataTable } from "../charts.js";
import { correctionForm, claimForm, ruleForm, withdrawCorrection } from "../forms.js";

const TABS = [["corrections", "Corrections & rules"], ["claims", "Notes & claims"], ["audit", "Audit trail"]];

export async function render(container, { params, isCurrent = () => true }) {
  const body = h("div");
  const u = currentUser();
  mount(container, h("div", { class: "page-head" }, h("div", null, h("h2", null, "Evidence & shared knowledge"),
    h("p", { class: "muted" }, "Corrections never rewrite the source data: the original is kept, who changed what and why is recorded, and the change applies at the stated scope only.")),
    h("div", { class: "row" }, h("button", { type: "button", class: "btn btn-primary", onclick: () => correctionForm({}) }, "Record a correction…"), h("button", { type: "button", class: "btn", onclick: () => claimForm({}) }, "Add a note or claim…"), h("button", { type: "button", class: "btn", onclick: () => ruleForm({}) }, "Propose a team rule…"))), body);
  mount(body, skeleton(6));
  let k;
  try { k = await api.knowledge(state.userId); } catch (e) { if (isCurrent()) mount(body, errorState(e, () => render(container, { params, isCurrent }))); return; }
  if (!isCurrent()) return;
  const tab = TABS.some(([t]) => t === params.tab) ? params.tab : "corrections";
  const panels = {};
  const tablist = h("div", { class: "subnav", role: "tablist", "aria-label": "Knowledge sections" });
  const show = (t) => { for (const [key] of TABS) { panels[key].hidden = key !== t; const b = tablist.querySelector(`[data-tab="${key}"]`); b.setAttribute("aria-selected", String(key === t)); } history.replaceState(null, "", link("knowledge", t === "corrections" ? {} : { tab: t })); };
  TABS.forEach(([t, label]) => tablist.append(h("button", { type: "button", role: "tab", dataset: { tab: t }, id: `kt-${t}`, "aria-controls": `kp-${t}`, onclick: () => show(t) }, label)));

  panels.corrections = h("div", null, h("p", { class: "small muted" }, `${k.corrections.length} on record. Select a reference to see the original value, the author, the reason and the audit history.`),
    k.corrections.length ? h("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": "Corrections (scrollable)" }, h("table", null, h("caption", { class: "sr-only" }, "Corrections and rule changes"),
      h("thead", null, h("tr", null, ["Status", "Kind", "Applies to", "Field", "Original", "Now", "By", "When", "Reason", ""].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", null, k.corrections.map((c) => h("tr", null, h("td", null, chip(c.status), c.orphaned ? [" ", tonedChip("warn", "target no longer in the data")] : null), h("td", null, c.kind.replace(/_/g, " ")), h("td", null, `${c.scope_kind}: ${c.scope_value}`), h("td", null, c.field),
        h("td", null, JSON.stringify(c.original_value)), h("td", null, JSON.stringify(c.new_value)), h("td", null, c.author), h("td", null, fmt.dt(c.created_at)), h("td", null, c.reason || ""),
        h("td", null, refChip(c.ref, onRef, c.correction_id), c.status === "active" ? [" ", h("button", { type: "button", class: "link-btn", onclick: () => withdrawCorrection(c.correction_id) }, "Withdraw")] : null)))))) : emptyState("No corrections yet", "Use “Record a correction” or tell the copilot in the Investigate view."));

  panels.claims = h("div", null, h("p", { class: "small muted" }, `Shared claims are visible to everyone. Private claims are visible only to their author${u ? ` (you are ${u.name})` : ""}. A claim stays unconfirmed unless the data supports it, and conflicting claims are kept side by side.`),
    k.claims.length ? h("ul", { class: "plain" }, k.claims.map((c) => h("li", { class: "section" }, tonedChip(c.visibility === "private" ? "unknown" : "info", c.visibility === "private" ? "Private" : "Shared"), " ", chip(c.verification), " ",
      h("a", { href: link(`assets/${c.asset_id}`, { tab: "knowledge" }) }, c.asset_id), ` · ${c.author} · ${fmt.dt(c.created_at)}: ${c.text}`, c.verification_detail ? h("div", { class: "small muted" }, `Checked against the data: ${c.verification_detail}`) : null, " ", refChip(c.ref, onRef, c.claim_id)))) : emptyState("No claims visible to you"));

  panels.audit = h("div", null, h("p", { class: "small muted" }, "Every change to corrections, claims and rules is logged with who, what and when. Another user's private claims and conversations never appear here."),
    k.audit.length ? dataTable({ title: "Audit trail", columns: [{ key: "at", label: "When (UTC)" }, { key: "actor", label: "Who" }, { key: "action", label: "What" }, { key: "object", label: "Object" }, { key: "detail", label: "Detail" }],
      rows: k.audit.map((a) => ({ at: fmt.dt(a.at), actor: a.actor, action: a.action.replace(/_/g, " "), object: `${a.object_type} ${a.object_id}`, detail: Object.entries(a.detail || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length)).map(([kk, v]) => `${kk}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("; ").slice(0, 220),
        _ref: ["correction", "claim"].includes(a.object_type) ? `${a.object_type}:${a.object_id}` : null })), onRef }) : emptyState("Nothing recorded yet"));

  mount(body, tablist, ...TABS.map(([t]) => { panels[t].id = `kp-${t}`; panels[t].setAttribute("role", "tabpanel"); panels[t].setAttribute("aria-labelledby", `kt-${t}`); return panels[t]; }));
  show(tab);
  announce("Evidence and knowledge loaded");
}
