// DOM helpers. Nothing here ever uses innerHTML: every value from the backend or the model is inserted as text.

const SVG_NS = "http://www.w3.org/2000/svg";

function appendKids(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  appendKids(el, kids);
  return el;
}

export function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, String(v));
  }
  appendKids(el, kids);
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(container, ...kids) { clear(container); appendKids(container, kids); return container; }

// ---------------------------------------------------------------- formatting
export const fmt = {
  num(v, digits = 1) { return v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits }); },
  usd(v) { return v === null || v === undefined ? "—" : "USD " + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 }); },
  date(iso) { return iso ? String(iso).slice(0, 10) : "—"; },
  dt(iso) { return iso ? String(iso).replace("T", " ").replace(/:\d\dZ$/, "Z").replace("Z", " UTC") : "—"; },
  ago(iso, asOf) {
    if (!iso || !asOf) return "";
    const d = Math.round((Date.parse(asOf + "T00:00:00Z") - Date.parse(iso.slice(0, 10) + "T00:00:00Z")) / 86400000);
    return d <= 0 ? "today" : d === 1 ? "1 day before the data date" : `${d} days before the data date`;
  },
};

const STATUS = {
  producing: ["ok", "●", "Producing"], not_producing: ["bad", "■", "Not producing"], unknown: ["unknown", "?", "Unknown"],
  ok: ["ok", "●", "OK"], warn: ["warn", "▲", "Attention"], bad: ["bad", "■", "Problem"], info: ["info", "◆", "Info"],
  high: ["bad", "■", "High"], medium: ["warn", "▲", "Medium"],
  breached: ["bad", "■", "Breached"], pending: ["warn", "▲", "Pending"], met: ["ok", "●", "Met"], unconfirmed: ["warn", "▲", "Unconfirmed"],
  supported: ["ok", "●", "Supported"], contradicted: ["bad", "■", "Contradicted"], active: ["info", "◆", "Active"], withdrawn: ["unknown", "–", "Withdrawn"], superseded: ["unknown", "–", "Superseded"],
};
export function chip(kind, label) {
  const [tone, icon, text] = STATUS[kind] || ["unknown", "?", kind];
  return h("span", { class: `chip chip-${tone}` }, h("span", { "aria-hidden": "true" }, icon), label || text);
}
export function tonedChip(tone, label) {
  const icon = { ok: "●", warn: "▲", bad: "■", info: "◆", unknown: "?" }[tone] || "◆";
  return h("span", { class: `chip chip-${tone}` }, h("span", { "aria-hidden": "true" }, icon), label);
}
export function kindChip(label) { return h("span", { class: "chip chip-kind" }, label); }

// ---------------------------------------------------------------- notices
export function notice(tone, title, items, extra) {
  const icon = { warn: "▲", bad: "■", info: "◆", ok: "●" }[tone] || "◆";
  const list = Array.isArray(items) ? items.filter(Boolean) : items ? [items] : [];
  return h("div", { class: `notice notice-${tone}`, role: tone === "bad" ? "alert" : "note" },
    h("span", { class: "notice-icon", "aria-hidden": "true" }, icon),
    h("div", null, title ? h("h4", null, title) : null,
      list.length === 1 ? h("span", null, list[0]) : list.length ? h("ul", null, list.map((x) => h("li", null, x))) : null, extra));
}

// ---------------------------------------------------------------- citations / refs
export function refChip(ref, onRef, label) {
  const [type, ...rest] = String(ref).split(":");
  return h("button", { type: "button", class: "ref-chip", dataset: { ref }, title: `Open evidence ${ref}`, "aria-label": `Open evidence: ${label || ref}`, onclick: () => onRef && onRef(ref) },
    label || `${type}:${rest.join(":")}`);
}

// ---------------------------------------------------------------- small, safe markdown (bold, italics, code, lists, pipe tables, citations)
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[a-z_]+:[^\]\s]+\]|(?<![\w*])\*[^*\s][^*]*\*(?![\w*]))/g;

export function inline(text, onRef, knownRefs) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(h("strong", null, tok.slice(2, -2)));
    else if (tok.startsWith("`")) out.push(h("code", null, tok.slice(1, -1)));
    else if (tok.startsWith("[")) {
      const ref = tok.slice(1, -1);
      // only a reference the backend actually returned as evidence becomes clickable
      out.push(!knownRefs || knownRefs.has(ref) ? refChip(ref, onRef) : tok);
    } else out.push(h("em", null, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function markdown(text, { onRef, knownRefs } = {}) {
  const root = h("div", { class: "answer" });
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\s*\|/.test(line) && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1] || "")) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      root.append(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, head.map((c) => h("th", { scope: "col" }, inline(c, onRef, knownRefs))))),
        h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, inline(c, onRef, knownRefs)))))))));
      continue;
    }
    if (/^\s*([*-]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([*-]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([*-]|\d+\.)\s+/, ""));
      root.append(h(ordered ? "ol" : "ul", null, items.map((t) => h("li", null, inline(t, onRef, knownRefs)))));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*([*-]|\d+\.)\s+/.test(lines[i]) && !/^\s*\|/.test(lines[i])) para.push(lines[i++]);
    root.append(h("p", null, inline(para.join(" "), onRef, knownRefs)));
  }
  return root;
}

// ---------------------------------------------------------------- misc
export function announce(msg) { const l = document.getElementById("live"); if (l) { l.textContent = ""; setTimeout(() => (l.textContent = msg), 30); } }
export function toast(msg, ms = 5000) {
  const box = document.getElementById("toasts");
  const t = h("div", { class: "toast", role: "status" }, msg);
  box.append(t);
  setTimeout(() => t.remove(), ms);
}
export function skeleton(lines = 3) { return h("div", { "aria-busy": "true", "aria-label": "Loading" }, Array.from({ length: lines }, (_, i) => h("div", { class: "skeleton", style: null, "data-w": i }, " ")).map((n) => { n.style.marginBottom = ".6rem"; n.style.width = `${90 - ((n.dataset.w * 17) % 40)}%`; return n; })); }
export function emptyState(title, body, action) { return h("div", { class: "empty" }, h("p", null, h("strong", null, title)), body ? h("p", { class: "muted" }, body) : null, action); }
export function errorState(err, retry) {
  const msg = err && err.message ? err.message : String(err);
  return notice("bad", "Could not load this", [msg + (err && err.code ? ` (${err.code})` : "")], retry ? h("button", { type: "button", class: "btn", onclick: retry }, "Try again") : null);
}
