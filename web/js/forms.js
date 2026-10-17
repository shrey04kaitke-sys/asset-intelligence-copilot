// Forms for corrections, claims and rule changes. Every action posts to a real endpoint and shows the backend's own verdict.
import { h, mount, notice, tonedChip, chip, toast, announce, fmt } from "./dom.js";
import { api } from "./api.js";
import { state, currentUser } from "./state.js";
import { openEvidence } from "./evidence.js";

const modal = () => document.getElementById("modal");

function open(content) {
  const m = modal();
  mount(m, h("div", { class: "modal-inner" }, content));
  if (!m.open) m.showModal();
  const first = m.querySelector("input, select, textarea, button");
  if (first) first.focus();
  return m;
}
export function closeModal() { const m = modal(); if (m.open) m.close(); }

function field(id, label, control, hint) {
  return h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("span", { class: "hint", id: id + "-hint" }, hint) : null);
}
function requireUser() {
  if (!state.userId) { toast("Choose who you are in the top-right picker first."); return false; }
  return true;
}

// ---------------------------------------------------------------- confirmation
export function confirmDialog({ title, body, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const m = open([
      h("h2", { id: "modal-title" }, title), h("div", null, body),
      h("div", { class: "modal-actions" },
        h("button", { type: "button", class: "btn", onclick: () => { m.close(); resolve(false); } }, "Cancel"),
        h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, onclick: () => { m.close(); resolve(true); } }, confirmLabel))]);
    m.addEventListener("close", () => resolve(false), { once: true });
  });
}

function resultPanel(res, extra) {
  const bits = [];
  if (res.needs_confirmation && res.needs_confirmation.length) bits.push(notice("warn", "Needs your confirmation", res.needs_confirmation));
  if (res.warnings && res.warnings.length) bits.push(notice("warn", "Please note", res.warnings));
  return bits.concat(extra || []);
}

// ---------------------------------------------------------------- correction of an inspection value
export function correctionForm(pre = {}) {
  if (!requireUser()) return;
  const fields = { wellhead_temp_c: "Wellhead temperature (°C) — raw data", ambient_temp_c: "Ambient air temperature (°C) — raw data", spill_est_volume_bbl: "Estimated spill volume (bbl) — AI analysis",
    spill_area_pct: "Spill area (% of pad) — raw data", spill_detected: "Spill detected (true/false)" };
  const msg = h("div", { "aria-live": "polite" });
  const id = h("input", { id: "f-insp", type: "text", value: pre.inspection_id || "", placeholder: "e.g. INS-0164-0205", autocomplete: "off" });
  const asset = h("input", { id: "f-asset", type: "text", value: pre.asset_id || "", placeholder: "well id", autocomplete: "off" });
  const date = h("input", { id: "f-date", type: "date" });
  const fieldSel = h("select", { id: "f-field" }, Object.entries(fields).map(([k, v]) => h("option", { value: k, selected: pre.field === k }, v)));
  const val = h("input", { id: "f-val", type: "text", inputmode: "decimal", required: true, autocomplete: "off" });
  const why = h("textarea", { id: "f-why", rows: 3, required: true, minlength: 3, maxlength: 300, placeholder: "Why is the recorded value wrong, and how do you know the right one?" });
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "Review and record…");
  const form = h("form", { class: "form", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    const raw = val.value.trim();
    const isBool = fieldSel.value === "spill_detected";
    const newValue = isBool ? ["true", "yes", "1"].includes(raw.toLowerCase()) : Number(raw);
    if (!raw || (!isBool && Number.isNaN(newValue))) { mount(msg, notice("bad", "Enter a numeric value", [])); return; }
    if (!id.value.trim() && !(asset.value.trim() && date.value)) { mount(msg, notice("bad", "Which inspection?", ["Give an inspection id, or a well and a date."])); return; }
    if (why.value.trim().length < 3) { mount(msg, notice("bad", "A reason is required", ["Corrections without a reason are rejected."])); return; }
    const ok = await confirmDialog({ title: "Record this correction?", confirmLabel: "Record correction",
      body: [h("p", null, `${fields[fieldSel.value]} will be treated as `, h("strong", null, String(newValue)), " in every answer for every user, for this one inspection only."),
        h("p", { class: "muted small" }, "The original value is kept, and your name, the time and your reason are written to the audit log. You can withdraw it later.")] });
    open(content);                                  // the confirmation replaced the form; bring it back so the result is visible
    if (!ok) return;
    submit.disabled = true;
    try {
      const r = (await api.postCorrection({ user_id: state.userId, field: fieldSel.value, new_value: newValue, reason: why.value.trim(), inspection_id: id.value.trim() || null, asset_id: asset.value.trim() || null, date: date.value || null, confirmed: true })).result;
      announce("Correction recorded");
      mount(msg, notice("ok", `Recorded ${r.correction.correction_id}`, [`${r.inspection_id}: ${fieldSel.value} ${r.original_value} → ${newValue}. Original kept.`]),
        ...resultPanel(r, r.impact && r.impact.length ? [h("div", null, h("h4", null, "Effect on the analysis"), h("ul", null, r.impact.map((i) => h("li", null, i))))] : []),
        h("p", null, h("button", { type: "button", class: "link-btn", onclick: () => { closeModal(); openEvidence(`correction:${r.correction.correction_id}`); } }, "Open the correction record")));
      window.dispatchEvent(new CustomEvent("copilot:data-changed"));
      submit.textContent = "Recorded";
    } catch (err) { submit.disabled = false; mount(msg, notice("bad", err.status === 403 ? "You are not allowed to do this" : "The correction was not recorded", [err.message])); }
  } },
    field("f-insp", "Inspection id", id, "If you know it."), h("div", { class: "field-row" }, field("f-asset", "…or well", asset), field("f-date", "…and inspection date", date)),
    field("f-field", "What is wrong?", fieldSel), field("f-val", "Correct value", val, "Booleans: true or false."), field("f-why", "Reason (required)", why), msg,
    h("div", { class: "modal-actions" }, h("button", { type: "button", class: "btn", onclick: closeModal }, "Close"), submit));
  const content = [h("h2", { id: "modal-title" }, "Correct a recorded value"), h("p", { class: "muted small" }, `Recording as ${currentUser()?.name || "?"}. Corrections apply to one inspection and never rewrite the source data.`), form];
  open(content);
}

// ---------------------------------------------------------------- claim / note
export function claimForm(pre = {}) {
  if (!requireUser()) return;
  const types = { planned_shutdown: "A planned shutdown", status_not_producing: "The well is shut in / not producing", status_producing: "The well is running / producing", observation: "Another observation" };
  const msg = h("div", { "aria-live": "polite" });
  const asset = h("input", { id: "c-asset", type: "text", value: pre.asset_id || "", required: true, placeholder: "well id", autocomplete: "off" });
  const type = h("select", { id: "c-type" }, Object.entries(types).map(([k, v]) => h("option", { value: k }, v)));
  const text = h("textarea", { id: "c-text", rows: 3, required: true, minlength: 3, maxlength: 500, placeholder: pre.text || "Describe what you know, in your own words." });
  if (pre.text) text.value = pre.text;
  const vf = h("input", { id: "c-from", type: "date" }), vt = h("input", { id: "c-to", type: "date" });
  const vis = h("select", { id: "c-vis" }, h("option", { value: "shared" }, "Shared — everyone sees it"), h("option", { value: "private" }, "Private — only you see it"));
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "Record");
  const content = [h("h2", { id: "modal-title" }, "Add a note or claim"), h("p", { class: "muted small" }, `Recording as ${currentUser()?.name || "?"}. A claim is checked against the latest inspection and stays marked unconfirmed unless the data supports it. Conflicting claims are kept side by side.`),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault(); submit.disabled = true;
      try {
        const r = (await api.postClaim({ user_id: state.userId, asset_id: asset.value.trim(), text: text.value.trim(), claim_type: type.value, valid_from: vf.value || null, valid_to: vt.value || null, visibility: vis.value })).result;
        announce("Claim recorded");
        const tone = r.verification === "supported" ? "ok" : r.verification === "contradicted" ? "bad" : "warn";
        mount(msg, notice(tone, `Recorded ${r.claim.claim_id} (${vis.value}) — ${r.verification}`, [r.detail || "Nothing in the data can confirm or contradict this yet."]),
          ...(r.conflicting_claims.length ? [notice("warn", "Conflicts with other statements", [r.conflicting_claims.join(", ") + ". Both stay on record and nothing is silently overridden."])] : []),
          ...(r.needs_confirmation ? [notice("info", "Marked unconfirmed", ["A colleague with first-hand knowledge can confirm or correct it."])] : []));
        window.dispatchEvent(new CustomEvent("copilot:data-changed"));
      } catch (err) { submit.disabled = false; mount(msg, notice("bad", "The claim was not recorded", [err.message])); }
    } }, field("c-asset", "Well", asset), field("c-type", "What are you recording?", type), field("c-text", "Statement", text),
      h("div", { class: "field-row" }, field("c-from", "Valid from (optional)", vf), field("c-to", "Valid to (optional)", vt)), field("c-vis", "Who can see it", vis), msg,
      h("div", { class: "modal-actions" }, h("button", { type: "button", class: "btn", onclick: closeModal }, "Close"), submit))];
  open(content);
}

// ---------------------------------------------------------------- team rule
export function ruleForm(pre = {}) {
  if (!requireUser()) return;
  const keys = { "rca.min_corrective_events": "Corrective repairs that trigger a root-cause analysis", "rca.window_months": "Look-back window for those repairs (months)" };
  const u = currentUser();
  const msg = h("div", { "aria-live": "polite" });
  const key = h("select", { id: "r-key" }, Object.entries(keys).map(([k, v]) => h("option", { value: k }, v)));
  const value = h("input", { id: "r-val", type: "number", step: "1", min: "1", required: true });
  const scope = h("select", { id: "r-scope" }, h("option", { value: "well_type" }, "Every well of a type"), h("option", { value: "asset" }, "One well"));
  const sv = h("input", { id: "r-sv", type: "text", required: true, placeholder: "well type or well id", value: pre.scope_value || "", autocomplete: "off" });
  const why = h("textarea", { id: "r-why", rows: 3, required: true, minlength: 3, maxlength: 300, placeholder: "Why is the team changing this?" });
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "Review and apply…");
  const content = [h("h2", { id: "modal-title" }, "Propose a team rule"),
    notice("info", null, [`Only company-policy rules can be changed here (root-cause-analysis thresholds). Regulatory deadlines and the production definition come from the manuals and SOPs and cannot be overridden. Rules for a whole well type need a supervisor, HSE or maintenance lead — you are signed in as ${u?.name || "?"} (${(u?.role || "").replace(/_/g, " ")}).`]),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      const ok = await confirmDialog({ title: "Apply this rule?", confirmLabel: "Apply rule", danger: scope.value === "well_type",
        body: [h("p", null, h("strong", null, keys[key.value]), ` becomes `, h("strong", null, value.value), ` for ${scope.value === "well_type" ? "every " + sv.value.toUpperCase() + " well" : sv.value.toUpperCase()}.`),
          h("p", { class: "muted small" }, "Answers will say that this follows your rule, not the manual. Other well types keep the manual's value. The change is audited and can be withdrawn.")] });
      open(content);                                  // the confirmation replaced the form; bring it back so the result is visible
    if (!ok) return;
      submit.disabled = true;
      try {
        const r = (await api.postRule({ user_id: state.userId, rule_key: key.value, value: Number(value.value), scope: scope.value, scope_value: sv.value.trim(), reason: why.value.trim(), confirmed: true })).result;
        announce("Rule recorded");
        mount(msg, notice("ok", `Recorded ${r.correction.correction_id}`, [`${r.rule_key}: ${r.original} (${r.source_of_original || "manual"}) → ${r.new} for ${r.scope}. ${r.note}`]), ...resultPanel(r));
        window.dispatchEvent(new CustomEvent("copilot:data-changed"));
      } catch (err) { submit.disabled = false; mount(msg, notice("bad", err.status === 403 ? "Not permitted for your role" : "The rule was not applied", [err.message])); }
    } }, field("r-key", "Which rule", key), field("r-val", "New value", value), field("r-scope", "Applies to", scope), field("r-sv", "Well type or well id", sv), field("r-why", "Reason (required)", why), msg,
      h("div", { class: "modal-actions" }, h("button", { type: "button", class: "btn", onclick: closeModal }, "Close"), submit))];
  open(content);
}

// ---------------------------------------------------------------- withdraw
export async function withdrawCorrection(correctionId) {
  if (!requireUser()) return;
  const why = h("textarea", { id: "w-why", rows: 2, maxlength: 300, placeholder: "Reason (optional)" });
  const ok = await confirmDialog({ title: `Withdraw ${correctionId}?`, confirmLabel: "Withdraw", danger: true,
    body: [h("p", null, "Answers go back to using the original value. The correction stays in the history marked as withdrawn."), field("w-why", "Reason", why)] });
  if (!ok) return;
  try {
    await api.withdraw({ user_id: state.userId, correction_id: correctionId, reason: why.value.trim() || null, confirmed: true });
    toast(`${correctionId} withdrawn.`); announce("Correction withdrawn");
    window.dispatchEvent(new CustomEvent("copilot:data-changed"));
  } catch (err) { toast(err.status === 403 ? "Only the author or a supervisor/HSE/maintenance lead can withdraw a correction." : err.message); }
}
