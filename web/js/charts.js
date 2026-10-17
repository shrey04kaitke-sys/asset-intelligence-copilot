// Renderers for the backend's validated visual specs. The browser draws; it never invents or recomputes values.
import { h, s, chip, tonedChip, kindChip, fmt, refChip, notice } from "./dom.js";

const TONE = { ok: "ok", warn: "warn", bad: "bad", info: "info", unknown: "unknown" };

export function renderVisual(spec, { onRef } = {}) {
  if (!spec || typeof spec !== "object") return null;
  const draw = { card, table, line, bar, map, heatmap, timeline, donut }[spec.type];
  if (!draw) return notice("warn", "This visual type is not supported by the interface", [String(spec.type)]);
  const fig = h("figure", { class: "figure", "aria-label": spec.alt_text || spec.title });
  if (spec.type !== "card" && spec.type !== "table") fig.append(h("figcaption", null, spec.title));
  fig.append(draw(spec, onRef));
  if (spec.type !== "card" && spec.notes && spec.notes.length) fig.append(h("p", { class: "notes" }, spec.notes.join(" · ")));    // a card's notes duplicate the warnings shown beside it
  if (spec.skipped_without_coordinates && spec.skipped_without_coordinates.length) fig.append(h("p", { class: "notes" }, `Not shown (no coordinates): ${spec.skipped_without_coordinates.join(", ")}`));
  return fig;
}

// ---------------------------------------------------------------- card
function card(spec, onRef) {
  const tone = TONE[spec.status] || "info";
  const fields = h("dl", { class: "fields" }, spec.fields.flatMap((f) => [
    h("dt", null, f.label),
    h("dd", null, f.status && f.status !== "info" ? tonedChip(TONE[f.status] || "info", `${f.value ?? "—"}${f.unit ? " " + f.unit : ""}`) : `${f.value ?? "—"}${f.unit ? " " + f.unit : ""}`,
      f.ref ? [" ", refChip(f.ref, onRef, "source")] : null)]));
  return h("section", { class: `card status-card ${tone}`, "aria-label": spec.title },
    h("div", { class: "row" }, h("h3", { style: null }, spec.title), h("span", { class: "spacer" }), tonedChip(tone, spec.headline)),
    spec.badges && spec.badges.length ? h("div", { class: "row small" }, spec.badges.map((b) => tonedChip("warn", b))) : null, fields,
    null);
}

// ---------------------------------------------------------------- table (sortable, filterable, paged)
export function dataTable({ title, columns, rows, onRef, pageSize = 25, filterable = true, caption }) {
  let sortKey = null, asc = true, page = 1, q = "";
  const wrap = h("div");
  const body = h("tbody");
  const info = h("span", { class: "muted small", role: "status" });
  const filterBox = h("input", { type: "search", "aria-label": `Filter ${title || "rows"}`, placeholder: "Filter rows…", oninput: (e) => { q = e.target.value.toLowerCase(); page = 1; draw(); } });
  const more = h("button", { type: "button", class: "btn", onclick: () => { page++; draw(); } }, "Show more");
  const headRow = h("tr");
  const isNum = (c) => rows.some((r) => typeof r[c.key] === "number");
  columns.forEach((c) => {
    const th = h("th", { scope: "col", class: isNum(c) ? "num" : "", "aria-sort": "none" },
      h("button", { type: "button", onclick: () => { asc = sortKey === c.key ? !asc : true; sortKey = c.key; headRow.querySelectorAll("th").forEach((t) => t.setAttribute("aria-sort", "none")); th.setAttribute("aria-sort", asc ? "ascending" : "descending"); page = 1; draw(); }, "aria-label": `Sort by ${c.label}` },
        c.label + (c.unit ? ` (${c.unit})` : ""), h("span", { "aria-hidden": "true" }, "↕")));
    headRow.append(th);
  });
  function view() {
    let r = rows.filter((x) => !q || columns.some((c) => String(x[c.key] ?? "").toLowerCase().includes(q)));
    if (sortKey) r = [...r].sort((a, b) => { const x = a[sortKey], y = b[sortKey]; const c = typeof x === "number" && typeof y === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""), undefined, { numeric: true }); return asc ? c : -c; });
    return r;
  }
  function draw() {
    const r = view();
    body.replaceChildren(...r.slice(0, page * pageSize).map((row) => {
      const tr = h("tr");
      columns.forEach((c, i) => {
        const v = row[c.key];
        const td = h("td", { class: typeof v === "number" ? "num" : "" });
        const content = typeof v === "number" ? fmt.num(v, 3) : (v === null || v === undefined || v === "" ? "—" : String(v));
        if (i === 0 && row._ref && onRef) td.append(h("button", { type: "button", class: "link-btn", onclick: () => onRef(row._ref), "aria-label": `Open evidence for ${content}` }, content));
        else td.append(content);
        tr.append(td);
      });
      return tr;
    }));
    more.hidden = r.length <= page * pageSize;
    info.textContent = `${Math.min(r.length, page * pageSize)} of ${r.length} rows${r.length !== rows.length ? ` (filtered from ${rows.length})` : ""}`;
  }
  wrap.append(
    rows.length > 8 && filterable ? h("div", { class: "table-tools" }, filterBox, info) : h("div", { class: "table-tools" }, info),
    h("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": `${title || "Table"} (scrollable)` },
      h("table", null, caption || title ? h("caption", null, caption || title) : null, h("thead", null, headRow), body)),
    more);
  draw();
  return wrap;
}

function table(spec, onRef) {
  if (!spec.rows.length) return notice("info", spec.title, ["No rows match."]);
  const wrap = dataTable({ title: spec.title, columns: spec.columns, rows: spec.rows, onRef });
  if (spec.total_rows && spec.total_rows > spec.rows.length) wrap.append(h("p", { class: "small muted" }, `Showing ${spec.rows.length} of ${spec.total_rows} rows.`));
  return wrap;
}

function dataToggle(label, node) { return h("details", { class: "data-toggle" }, h("summary", null, label), node); }
const nice = (lo, hi, n = 5) => {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0))), step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= step0) || step0;
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, ticks = [];
  for (let v = a; v <= b + 1e-9; v += step) ticks.push(+v.toFixed(6));
  return { lo: a, hi: b, ticks };
};

// ---------------------------------------------------------------- line
function line(spec, onRef) {
  const W = 760, H = 320, m = { l: 52, r: 16, t: 14, b: 54 };
  const xs = spec.series[0] ? spec.series[0].points.map((p) => p.x) : [];
  const ys = spec.series.flatMap((x) => x.points.map((p) => p.y)).filter((v) => v !== null && v !== undefined);
  if (!xs.length || !ys.length) return notice("info", spec.title, ["No readings in this period."]);
  const sc = nice(Math.min(...ys), Math.max(...ys));
  const X = (i) => m.l + (xs.length === 1 ? (W - m.l - m.r) / 2 : (i * (W - m.l - m.r)) / (xs.length - 1));
  const Y = (v) => m.t + (1 - (v - sc.lo) / (sc.hi - sc.lo)) * (H - m.t - m.b);
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.alt_text });
  sc.ticks.forEach((t) => svg.append(s("line", { class: "grid-line", x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t) }), s("text", { x: m.l - 6, y: Y(t) + 4, "text-anchor": "end" }, String(t))));
  const every = Math.max(1, Math.ceil(xs.length / 8));
  xs.forEach((x, i) => { if (i % every === 0 || (i === xs.length - 1 && (xs.length - 1) % every >= Math.ceil(every / 2))) svg.append(s("text", { x: X(i), y: H - m.b + 16, "text-anchor": "end", transform: `rotate(-35 ${X(i)} ${H - m.b + 16})` }, x)); });
  svg.append(s("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }), s("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: H - m.b }),
    s("text", { x: 12, y: m.t + 10, transform: `rotate(-90 12 ${m.t + 10})`, "text-anchor": "end" }, spec.y_label));
  const mx = new Set((spec.markers || []).map((k) => k.x));
  (spec.markers || []).forEach((k) => { const i = xs.indexOf(k.x); if (i >= 0) svg.append(s("line", { class: "marker", x1: X(i), x2: X(i), y1: m.t, y2: H - m.b, "stroke-width": 1.5, "stroke-dasharray": "3 3", opacity: 0.55 }),
    s("path", { class: "marker pt", d: `M${X(i) - 6},${H - m.b + 1} L${X(i) + 6},${H - m.b + 1} L${X(i)},${H - m.b - 10} Z`, tabindex: k.ref && onRef ? 0 : null, role: k.ref ? "button" : null, "aria-label": `${k.label} on ${k.x}`, onclick: k.ref && onRef ? () => onRef(k.ref) : null,
      onkeydown: k.ref && onRef ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRef(k.ref); } } : null }, s("title", null, `${k.label} — ${k.x}`))); });
  spec.series.forEach((ser, si) => {
    const cls = `s${si % 3}${si % 3 ? ` dash-${si % 3}` : ""}`;
    let seg = [];
    const flush = () => { if (seg.length > 1) svg.append(s("polyline", { class: `${cls} ${ser.style === "dashed" ? "dash-1" : ""}`, points: seg.join(" ") })); seg = []; };
    ser.points.forEach((p, i) => { if (p.y === null || p.y === undefined) flush(); else seg.push(`${X(i)},${Y(p.y)}`); });
    flush();
    ser.points.forEach((p, i) => { if (p.y !== null && p.y !== undefined && (xs.length <= 40 || mx.has(p.x))) svg.append(s("circle", { class: `${cls} pt`, cx: X(i), cy: Y(p.y), r: 3.2, tabindex: p.ref && onRef ? 0 : null, role: p.ref ? "button" : null,
      "aria-label": `${ser.name} ${p.x}: ${p.y}`, onclick: p.ref && onRef ? () => onRef(p.ref) : null, onkeydown: p.ref && onRef ? (e) => { if (e.key === "Enter") onRef(p.ref); } : null }, s("title", null, `${ser.name}, ${p.x}: ${p.y}${ser.unit ? " " + ser.unit : ""}`))); });
  });
  const legend = h("ul", { class: "legend" }, spec.series.map((ser, si) => h("li", null, s("svg", { width: 30, height: 10, "aria-hidden": "true" }, s("line", { x1: 0, x2: 30, y1: 5, y2: 5, class: `s${si % 3}`, "stroke-width": 2.5, "stroke-dasharray": ser.style === "dashed" || si % 3 === 1 ? "7 4" : si % 3 === 2 ? "2 3" : null })), ser.name)),
    spec.markers && spec.markers.length ? h("li", null, s("svg", { width: 14, height: 12, "aria-hidden": "true" }, s("path", { class: "marker", d: "M1,11 L13,11 L7,1 Z" })), `${spec.markers[0].label} (${spec.markers.length})`) : null);
  const data = dataTable({ title: spec.title, caption: `${spec.title} — values`, filterable: false, pageSize: 60,
    columns: [{ key: "x", label: spec.x_label }, ...spec.series.map((x, i) => ({ key: "s" + i, label: x.name, unit: x.unit })), { key: "mark", label: "Marked" }],
    rows: xs.map((x, i) => ({ x, ...Object.fromEntries(spec.series.map((ser, si) => ["s" + si, ser.points[i] ? ser.points[i].y : null])), mark: mx.has(x) ? spec.markers.find((k) => k.x === x).label : "", _ref: (spec.series[0].points[i] || {}).ref })), onRef });
  return h("div", null, svg, legend, dataToggle("Show the values as a table", data));
}

// ---------------------------------------------------------------- bar (horizontal)
function bar(spec, onRef) {
  const vals = spec.series[0].values;
  const max = Math.max(1e-9, ...vals.filter((v) => v !== null));
  const list = h("div", { role: "img", "aria-label": spec.alt_text }, spec.categories.map((c, i) => {
    const v = vals[i];
    const fill = h("div", { class: "bar-fill" });
    fill.style.width = `${v === null ? 0 : Math.max(1, (v / max) * 100)}%`;
    return h("div", { class: "bar-row" }, h("span", { title: c }, spec.annotations && spec.annotations[i] ? spec.annotations[i] : c), h("div", { class: "bar-track" }, fill), h("span", { class: "mono" }, v === null ? "n/a" : `${fmt.num(v, 3)}${spec.unit ? " " + spec.unit : ""}`));
  }));
  const data = dataTable({ title: spec.title, caption: `${spec.title} — values`, filterable: false, columns: [{ key: "c", label: "Category" }, { key: "v", label: spec.value_label, unit: spec.unit }], rows: spec.categories.map((c, i) => ({ c, v: vals[i] })), onRef });
  return h("div", null, list, dataToggle("Show the values as a table", data));
}

// ---------------------------------------------------------------- map (schematic position plot; no basemap, no invented geography)
function map(spec, onRef) {
  if (!spec.points.length) return notice("info", spec.title, ["No locations to show."]);
  const W = 640, H = 420, m = { l: 56, r: 24, t: 20, b: 44 };
  const lats = spec.points.map((p) => p.lat), lons = spec.points.map((p) => p.lon);
  let [la0, la1, lo0, lo1] = [Math.min(...lats), Math.max(...lats), Math.min(...lons), Math.max(...lons)];
  const padLa = Math.max((la1 - la0) * 0.12, 0.002), padLo = Math.max((lo1 - lo0) * 0.12, 0.002);
  la0 -= padLa; la1 += padLa; lo0 -= padLo; lo1 += padLo;
  const kx = Math.cos(((la0 + la1) / 2) * Math.PI / 180);                     // equirectangular, corrected for latitude
  const wDeg = (lo1 - lo0) * kx, hDeg = la1 - la0, scale = Math.min((W - m.l - m.r) / wDeg, (H - m.t - m.b) / hDeg);
  const ox = m.l + ((W - m.l - m.r) - wDeg * scale) / 2, oy = m.t + ((H - m.t - m.b) - hDeg * scale) / 2;
  const X = (lon) => ox + (lon - lo0) * kx * scale, Y = (lat) => oy + (la1 - lat) * scale;
  const vmax = Math.max(1e-9, ...spec.points.map((p) => p.value || 0));
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.alt_text });
  for (let i = 0; i <= 3; i++) {
    const lat = la0 + ((la1 - la0) * i) / 3, lon = lo0 + ((lo1 - lo0) * i) / 3;
    svg.append(s("line", { class: "grid-line", x1: ox, x2: ox + wDeg * scale, y1: Y(lat), y2: Y(lat) }), s("text", { x: ox - 4, y: Y(lat) + 4, "text-anchor": "end" }, lat.toFixed(3)),
      s("line", { class: "grid-line", y1: oy, y2: oy + hDeg * scale, x1: X(lon), x2: X(lon) }), s("text", { x: X(lon), y: oy + hDeg * scale + 16, "text-anchor": "middle" }, lon.toFixed(3)));
  }
  svg.append(s("text", { x: ox + wDeg * scale, y: 14, "text-anchor": "end" }, "N ↑"));
  spec.points.forEach((p, i) => {
    const r = 6 + 8 * Math.sqrt((p.value || 0) / vmax), cx = X(p.lon), cy = Y(p.lat), tone = p.tone || "info";
    const common = { class: `pt s${tone === "bad" ? 1 : tone === "ok" ? 2 : 0}`, tabindex: onRef && p.ref ? 0 : null, role: p.ref ? "button" : "img", "aria-label": `${p.label}: ${p.detail || ""}`, onclick: onRef && p.ref ? () => onRef(p.ref) : null,
      onkeydown: onRef && p.ref ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRef(p.ref); } } : null, "fill-opacity": 0.78, stroke: "var(--surface)", "stroke-width": 1.5 };
    const shape = tone === "bad" ? s("rect", { ...common, x: cx - r, y: cy - r, width: 2 * r, height: 2 * r }) : tone === "warn" ? s("path", { ...common, d: `M${cx},${cy - r} L${cx + r},${cy + r} L${cx - r},${cy + r} Z` }) : s("circle", { ...common, cx, cy, r });
    shape.append(s("title", null, `${p.label} — ${p.detail || ""}`));
    svg.append(shape, s("text", { x: cx + r + 3, y: cy + (i % 2 ? 12 : -4), "font-weight": 600 }, p.label));
  });
  const legend = h("ul", { class: "legend" }, h("li", null, "Positions are plotted from the wells' recorded coordinates (schematic, no basemap)."), spec.value_label ? h("li", null, `Marker size ∝ ${spec.value_label}`) : null,
    h("li", null, "Shape + label identify each point: ■ severe/not producing, ▲ moderate, ● other."));
  const data = dataTable({ title: spec.title, caption: `${spec.title} — locations`, filterable: false, columns: [{ key: "label", label: "Well" }, { key: "lat", label: "Latitude" }, { key: "lon", label: "Longitude" }, { key: "value", label: spec.value_label || "Value" }, { key: "detail", label: "Detail" }],
    rows: spec.points.map((p) => ({ label: p.label, lat: p.lat, lon: p.lon, value: p.value, detail: p.detail, _ref: p.ref })), onRef });
  return h("div", null, svg, legend, dataToggle("Show the locations as a table", data));
}

// ---------------------------------------------------------------- heatmap (an HTML table: numbers are always printed in the cells)
function heatmap(spec) {
  const flat = spec.values.flat().filter((v) => v !== null);
  const max = Math.max(1, ...flat);
  const bucket = (v) => (v === null ? 0 : v === 0 ? 0 : Math.min(5, 1 + Math.floor((v / max) * 4.999)));
  const tbl = h("table", { class: "heat" }, h("caption", { class: "sr-only" }, spec.alt_text),
    h("thead", null, h("tr", null, h("th", { scope: "col" }, ""), spec.col_labels.map((c) => h("th", { scope: "col", class: "num" }, c)), spec.row_totals ? h("th", { scope: "col", class: "num" }, "Total") : null)),
    h("tbody", null, spec.row_labels.map((r, i) => h("tr", null, h("th", { scope: "row", class: "rowh" }, r), spec.values[i].map((v) => h("td", { class: `h${bucket(v)}` }, v === null ? "—" : fmt.num(v, 3))), spec.row_totals ? h("td", { class: "num" }, h("strong", null, fmt.num(spec.row_totals[i], 3))) : null))));
  return h("div", null, h("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": `${spec.title} (scrollable)` }, tbl), h("p", { class: "notes small muted" }, `Darker cell = higher count${spec.unit ? " of " + spec.unit : ""} (scale 0–${fmt.num(max, 0)}); every cell prints its value.`));
}

// ---------------------------------------------------------------- timeline
const KIND = { inspection: ["warn", "Inspection"], repair: ["info", "Repair"], spill: ["bad", "Spill"], regulatory: ["bad", "Regulator"], note: ["unknown", "Note"], claim: ["warn", "Claim"], other: ["unknown", "Event"] };
function timeline(spec, onRef) {
  if (!spec.events.length) return notice("info", spec.title, ["No events in this period."]);
  return h("ol", { class: "timeline", "aria-label": spec.title }, spec.events.map((e) => { const [tone, label] = KIND[e.kind] || KIND.other;
    return h("li", null, h("time", { datetime: e.date }, e.date), tonedChip(tone, label), " ", e.label, e.detail ? h("span", { class: "muted small" }, " — " + e.detail) : null, e.ref ? [" ", refChip(e.ref, onRef, "open")] : null); }));
}

// ---------------------------------------------------------------- donut
function donut(spec) {
  const total = spec.slices.reduce((a, b) => a + b.value, 0);
  if (!total) return notice("info", spec.title, ["Nothing to show."]);
  const R = 70, r = 42, cx = 90, cy = 90;
  let a0 = -Math.PI / 2;
  const svg = s("svg", { class: "chart", viewBox: "0 0 180 180", role: "img", "aria-label": spec.alt_text, style: null });
  svg.style.maxWidth = "220px";
  spec.slices.forEach((sl, i) => {
    const a1 = a0 + (sl.value / total) * 2 * Math.PI, big = a1 - a0 > Math.PI ? 1 : 0;
    const p = (rad, a) => `${cx + rad * Math.cos(a)},${cy + rad * Math.sin(a)}`;
    const d = sl.value === total ? `M${cx - R},${cy} a${R},${R} 0 1,0 ${2 * R},0 a${R},${R} 0 1,0 ${-2 * R},0 M${cx - r},${cy} a${r},${r} 0 1,1 ${2 * r},0 a${r},${r} 0 1,1 ${-2 * r},0` : `M${p(R, a0)} A${R},${R} 0 ${big} 1 ${p(R, a1)} L${p(r, a1)} A${r},${r} 0 ${big} 0 ${p(r, a0)} Z`;
    svg.append(s("path", { d, class: `s${i % 3}`, "fill-rule": "evenodd", "fill-opacity": 0.55 + (i % 3) * 0.15, stroke: "var(--surface)", "stroke-width": 2 }, s("title", null, `${sl.label}: ${sl.value}`)));
    a0 = a1;
  });
  svg.append(s("text", { x: cx, y: cy + 5, "text-anchor": "middle", "font-weight": 700, "font-size": 16 }, fmt.num(total, 0)));
  return h("div", { class: "row" }, svg, h("ul", { class: "legend", style: null }, spec.slices.map((sl, i) => h("li", null, h("strong", null, fmt.num(sl.value, 3)), " ", sl.label, ` (${Math.round((sl.value / total) * 100)}%)`))));
}
