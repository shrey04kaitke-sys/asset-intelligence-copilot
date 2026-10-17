import { h, mount, clear, markdown, chip, tonedChip, kindChip, notice, fmt, skeleton, emptyState, errorState, refChip, announce, toast } from "../dom.js";
import { api, ApiError } from "../api.js";
import { state, link, currentUser } from "../state.js";
import { openEvidence } from "../evidence.js";
import { renderVisual } from "../charts.js";
import { correctionForm, claimForm } from "../forms.js";

const ERROR_HELP = {
  rate_limited: "The hosted model is rate limiting requests. Wait a little and try again.",
  llm_unavailable: "The hosted model did not respond (server error or timeout). This is a provider problem, not a problem with your question.",
  llm_not_configured: "No model key is configured. Run `python scripts/setup_gemini_key.py` in a terminal on this computer.",
  llm_auth: "The model provider rejected the configured key.", truncated: "The model's reply was cut off and discarded rather than shown half-finished.",
  unknown_user: "Choose who you are in the top-right picker.", forbidden: "That conversation belongs to another user.",
};

export async function render(container, { params, isCurrent = () => true }) {
  let conversationId = params.c || null;
  const transcript = h("div", { class: "transcript", role: "log", "aria-label": "Investigation", "aria-live": "off" });
  const side = h("div");
  const input = h("textarea", { id: "q", rows: 3, maxlength: 2000, placeholder: "Ask about wells, inspections, repairs, spills, compliance… or tell the copilot a correction or a plan (for example: \"<well> will be shut in from 17 Oct for a planned workover\").", "aria-label": "Your question", "aria-describedby": "q-hint" });
  if (params.q) input.value = params.q;
  const send = h("button", { type: "submit", class: "btn btn-primary" }, "Ask");
  let busy = false, abort = null, lastQuestion = "";

  const composer = h("div", { class: "composer" }, h("form", { onsubmit: (e) => { e.preventDefault(); submit(input.value); } }, input,
    h("div", { class: "row" }, send, h("button", { type: "button", class: "btn btn-quiet", onclick: () => { conversationId = null; clear(transcript); transcript.append(intro()); history.replaceState(null, "", "#/investigate"); input.focus(); } }, "New investigation"),
      h("span", { class: "spacer" }), h("span", { id: "q-hint", class: "muted small" }, "Enter to send · Shift+Enter for a new line · answers cite real records"))));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(input.value); } });

  const intro = () => h("div", { class: "panel" }, h("h3", null, "Start an investigation"), h("p", { class: "muted" }, "Questions are answered by Gemma using validated backend tools; every number comes from deterministic code and every claim should cite a record you can open. Nothing is answered from memory."),
    h("p", { class: "small muted" }, "Decision support only: the copilot cannot control wells, drones or valves."));

  function userBubble(text) { return h("div", { class: "msg user" }, h("div", { class: "who-line" }, currentUser()?.name || "You"), h("p", null, text)); }

  function sources(resp) {
    const refs = new Map();
    [...resp.citations, ...resp.supporting].forEach((c) => refs.set(c.ref, c));
    const open = (r) => openEvidence(r, refs.get(r));
    const line = (c) => h("li", null, refChip(c.ref, open), " ", kindChip(c.kind), " ", c.title);
    return h("div", { class: "section" },
      resp.citations.length ? h("div", null, h("h4", null, `Cited in the answer (${resp.citations.length})`), h("ul", { class: "plain" }, resp.citations.map(line))) : notice("warn", "The answer cites no records", ["The text carries no valid citation. Treat it with caution; the records the tools returned are listed below."]),
      resp.supporting.length ? h("details", { open: !resp.citations.length }, h("summary", null, `Other records the tools returned (${resp.supporting.length}${resp.evidence_consulted > resp.citations.length + resp.supporting.length ? " of " + resp.evidence_consulted : ""})`), h("ul", { class: "plain" }, resp.supporting.map(line))) : null);
  }

  function assistant(resp) {
    const refs = new Map();
    [...resp.citations, ...resp.supporting].forEach((c) => refs.set(c.ref, c));
    const known = new Set(refs.keys());
    const el = h("div", { class: "msg assistant" });
    el.append(h("div", { class: "who-line" }, `Copilot · ${resp.model || "model"}${resp.thinking ? " · thinking " + resp.thinking : ""} · ${resp.usage.model_calls} model call${resp.usage.model_calls === 1 ? "" : "s"}, ${resp.usage.tool_calls} tool call${resp.usage.tool_calls === 1 ? "" : "s"} · ${resp.usage.seconds}s`));
    if (!resp.ok) {
      const e = resp.error || { code: "error", message: "Unknown error" };
      if (resp.actions_taken && resp.actions_taken.length) el.append(notice("warn", "Changes were already recorded before the failure", resp.actions_taken, h("span", { class: "small" }, "Do not send the same message again; check the Evidence & knowledge page.")));
      el.append(notice("bad", "No answer was produced", [`${e.message}`, ERROR_HELP[e.code] || ""], h("div", { class: "row" },
        h("button", { type: "button", class: "btn", onclick: () => submit(lastQuestion) }, "Try again"), e.retry_after_s ? h("span", { class: "small" }, `Suggested wait: about ${Math.ceil(e.retry_after_s)} s`) : null, h("span", { class: "small muted" }, `code: ${e.code}`))));
      el.append(trace(resp));
      return el;
    }
    el.append(markdown(resp.answer, { onRef: (r) => openEvidence(r, refs.get(r)), knownRefs: known }));
    if (resp.visual) el.append(renderVisual(resp.visual, { onRef: (r) => openEvidence(r) }));
    if (resp.visual_note) el.append(notice("info", null, [resp.visual_note]));
    if (resp.conflicts.length) el.append(notice("warn", "Sources disagree or are out of date", resp.conflicts));
    if (resp.uncertainty.length) el.append(notice("warn", "Uncertain or missing", resp.uncertainty));
    if (resp.actions_taken && resp.actions_taken.length) el.append(notice("ok", "Recorded by this request", resp.actions_taken));
    if (resp.needs_confirmation.length) el.append(notice("info", "Needs confirmation from you", resp.needs_confirmation));
    if (resp.pending_actions.length) resp.pending_actions.forEach((p) => el.append(notice("info", "Awaiting your confirmation", [`${p.summary}`], h("div", { class: "row" },
      h("button", { type: "button", class: "btn btn-primary", onclick: () => decide(p, true, el) }, "Confirm and record"), h("button", { type: "button", class: "btn", onclick: () => decide(p, false, el) }, "Decline")))));
    if (resp.assumptions.length) el.append(h("details", { class: "trace" }, h("summary", null, `Assumptions made (${resp.assumptions.length})`), h("ul", null, resp.assumptions.map((a) => h("li", null, a)))));
    if (resp.warnings.length) el.append(notice("warn", "Check before relying on this", resp.warnings));
    el.append(sources(resp));
    el.append(h("div", { class: "row" }, h("button", { type: "button", class: "btn", onclick: () => correctionForm({}) }, "Correct a value…"), h("button", { type: "button", class: "btn", onclick: () => claimForm({}) }, "Add a note or claim…")));
    el.append(trace(resp));
    return el;
  }

  function trace(resp) {
    return h("details", { class: "trace" }, h("summary", null, "How this answer was produced"),
      h("p", { class: "small muted" }, `Data date ${resp.data.as_of || "?"} (${resp.data.as_of_basis || ""}). Tokens: prompt ${resp.usage.prompt_tokens}, output ${resp.usage.output_tokens}, reasoning ${resp.usage.thought_tokens}. Reasoning text is never shown or stored.`),
      h("ol", null, resp.trace.map((t) => h("li", null, `${t.kind}${t.name ? " · " + t.name : ""} — ${t.ok ? "ok" : "failed" + (t.code ? " (" + t.code + ")" : "")}${t.rows !== null && t.rows !== undefined ? ", " + t.rows + " rows" : ""}${t.detail ? " — " + t.detail : ""} (${t.seconds}s)`))));
  }

  async function decide(p, approve, el) {
    try {
      const r = await api.confirm({ user_id: state.userId, proposal_id: p.proposal_id, approve });
      el.append(notice(r.status === "executed" ? "ok" : "info", r.status === "executed" ? "Recorded" : "Cancelled", [r.message]));
      window.dispatchEvent(new CustomEvent("copilot:data-changed"));
    } catch (e) { el.append(notice("bad", "Could not complete this", [e.message])); }
  }

  async function submit(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    if (!state.userId) { toast("Choose who you are in the top-right picker first."); return; }
    busy = true; send.disabled = true; lastQuestion = text;
    if (transcript.firstChild && transcript.firstChild.classList.contains("panel")) clear(transcript);
    transcript.append(userBubble(text));
    const t0 = Date.now();
    const timer = h("span", { class: "mono" }, "0s");
    const cancel = h("button", { type: "button", class: "btn btn-quiet", onclick: () => abort && abort.abort() }, "Stop waiting");
    const prog = h("div", { class: "progress", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("div", null, h("div", null, "Gemma is working: choosing tools, reading the records, writing the answer…"),
      h("div", { class: "small muted" }, "Elapsed ", timer, ". The hosted model can be slow or retry on its own; failures are shown, never replaced with a made-up answer.")), cancel);
    transcript.append(prog); prog.scrollIntoView({ block: "nearest" });
    const tick = setInterval(() => (timer.textContent = `${Math.round((Date.now() - t0) / 1000)}s`), 1000);
    announce("Working on your question");
    abort = new AbortController();
    input.value = "";
    try {
      const resp = await api.ask({ user_id: state.userId, question: text, conversation_id: conversationId }, abort.signal);
      prog.remove();
      if (resp.conversation_id) { conversationId = resp.conversation_id; history.replaceState(null, "", link("investigate", { c: conversationId })); }
      const node = assistant(resp);
      transcript.append(node);
      node.scrollIntoView({ block: "nearest" });
      announce(resp.ok ? "Answer ready" : "The request failed: " + (resp.error?.message || ""));
      if (resp.ok) window.dispatchEvent(new CustomEvent("copilot:conversations-changed"));
      if (resp.needs_confirmation && resp.needs_confirmation.length) announce("The copilot needs your confirmation");
    } catch (e) {
      prog.remove();
      if (e && e.name === "AbortError") transcript.append(notice("info", "Stopped waiting", ["The request was abandoned in the browser; the server may still finish it."], h("button", { type: "button", class: "btn", onclick: () => submit(text) }, "Ask again")));
      else transcript.append(notice("bad", e instanceof ApiError && e.status === 0 ? "Cannot reach the server" : "The request failed", [e.message], h("button", { type: "button", class: "btn", onclick: () => submit(text) }, "Try again")));
    } finally { clearInterval(tick); busy = false; send.disabled = false; input.focus(); }
  }

  async function loadSide() {
    mount(side, h("section", { class: "panel" }, h("h3", null, "Try a question"), skeleton(4)));
    let ex = [], convs = [];
    try { ex = await api.examples(); } catch (_) { /* examples are optional */ }
    if (state.userId) { try { convs = await api.conversations(state.userId); } catch (_) { /* optional */ } }
    if (!isCurrent()) return;
    mount(side, h("section", { class: "panel" }, h("h3", null, "Try a question"), h("p", { class: "small muted" }, "Generated from the loaded data. Selecting one only fills the box; you press Ask."),
      ex.length ? h("ul", { class: "examples" }, ex.map((e) => h("li", null, h("button", { type: "button", onclick: () => { input.value = e.question; input.focus(); } }, h("span", { class: "topic" }, e.topic), e.question)))) : emptyState("No examples", "Load a dataset first.")),
      h("section", { class: "panel", style: null }, h("h3", null, "Your recent investigations"), state.userId ? (convs.length ? h("ul", { class: "plain" }, convs.slice(0, 12).map((c) => h("li", null, h("a", { href: link("investigate", { c: c.conversation_id }) }, c.title || c.conversation_id), h("div", { class: "small muted" }, fmt.dt(c.started_at))))) : emptyState("None yet", "They are private to you.")) : emptyState("Choose who you are", "Investigations are private to each user.")));
  }

  async function loadConversation(id) {
    mount(transcript, skeleton(4));
    try {
      const msgs = await api.messages(id, state.userId);
      if (!isCurrent()) return;
      clear(transcript);
      if (!msgs.length) transcript.append(intro());
      msgs.forEach((m) => {
        if (m.role === "user") transcript.append(userBubble(m.content));
        else if (m.response && m.response.ok !== undefined) transcript.append(assistant(m.response));
        else transcript.append(h("div", { class: "msg assistant" }, markdown(m.content, {})));
      });
    } catch (e) { mount(transcript, errorState(e, () => loadConversation(id))); conversationId = null; }
  }

  mount(container, h("div", { class: "page-head" }, h("div", null, h("h2", null, "Investigate"), h("p", { class: "muted" }, "Ask in plain English. Answers cite the records they used; open any citation to inspect it."))),
    h("div", { class: "chat" }, h("div", null, transcript, composer), side));
  if (conversationId && state.userId) await loadConversation(conversationId); else transcript.append(intro());
  loadSide();
  const refresh = () => loadSide();
  window.addEventListener("copilot:conversations-changed", refresh);
  return () => window.removeEventListener("copilot:conversations-changed", refresh);
}
