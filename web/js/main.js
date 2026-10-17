import { h, mount, notice, errorState, skeleton } from "./dom.js";
import { api } from "./api.js";
import { state, setUser, storedUser, parseHash } from "./state.js";
import { initEvidence, configureEvidence, closeEvidence } from "./evidence.js";
import { correctionForm, withdrawCorrection } from "./forms.js";

const ROUTES = {
  overview: () => import("./views/overview.js"), assets: () => import("./views/assets.js"), investigate: () => import("./views/investigate.js"), knowledge: () => import("./views/knowledge.js"),
};
let cleanup = null;
let renderToken = 0;
let firstRender = true;

async function route() {
  const { parts, params } = parseHash();
  const name = parts[0] === "assets" && parts[1] ? "asset" : parts[0] || "overview";
  const main = document.getElementById("main");
  document.querySelectorAll("#nav a").forEach((a) => (a.dataset.route === (name === "asset" ? "assets" : name) ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  if (cleanup) { try { cleanup(); } catch (_) { /* ignore */ } cleanup = null; }
  const token = ++renderToken;
  const isCurrent = () => token === renderToken;                       // views must check this after every await so a slow, superseded render can never touch the page
  closeEvidence();
  try {
    const mod = name === "asset" ? await import("./views/asset.js") : ROUTES[name] ? await ROUTES[name]() : null;
    if (!mod) { mount(main, notice("warn", "Page not found", [`There is no page “${parts.join("/")}”.`], h("a", { href: "#/overview", class: "btn" }, "Go to the overview"))); return; }
    if (token !== renderToken) return;
    document.title = `${{ overview: "Fleet overview", assets: "Assets", asset: parts[1] || "Asset", investigate: "Investigate", knowledge: "Evidence & knowledge" }[name]} · Asset Intelligence Workspace`;
    const c = await mod.render(main, { parts, params, isCurrent });
    if (token === renderToken && typeof c === "function") cleanup = c;
    if (token === renderToken && !params.tab && !firstRender) main.focus({ preventScroll: true });     // after in-app navigation only: keeps the skip link the first Tab stop on load
    firstRender = false;
  } catch (e) { if (token === renderToken) mount(main, errorState(e, route)); }
}

async function boot() {
  initEvidence();
  configureEvidence({ correct: (pre) => correctionForm(pre), withdraw: (id) => withdrawCorrection(id) });
  const picker = document.getElementById("user-picker");
  const banner = document.getElementById("data-banner");
  try {
    const [health, users] = await Promise.all([api.health(), api.users()]);
    state.health = health; state.users = users;
    banner.textContent = health.data_loaded ? `${health.wells} wells · data as of ${health.data_date} · decision support only (no live drone/sensor/control integration)` : "No dataset loaded — run the ingestion first";
    const keyNote = health.model_key_available ? "" : " · model key NOT configured";
    banner.textContent += keyNote;
    users.forEach((u) => picker.append(h("option", { value: u.user_id }, `${u.name} — ${u.role.replace(/_/g, " ")}`)));
    const want = storedUser();
    setUser(users.some((u) => u.user_id === want) ? want : (users.find((u) => u.role === "production_supervisor") || users[0]).user_id);
    picker.value = state.userId;
  } catch (e) {
    banner.textContent = "Cannot reach the backend";
    mount(document.getElementById("main"), errorState(e, () => location.reload()));
    return;
  }
  picker.addEventListener("change", () => { setUser(picker.value); route(); });
  window.addEventListener("hashchange", route);
  let dirty = false;
  window.addEventListener("copilot:data-changed", () => { if (document.querySelector("dialog[open]")) dirty = true; else route(); });     // refresh once the form dialog is closed
  document.getElementById("modal").addEventListener("close", () => { if (dirty) { dirty = false; route(); } });
  route();
}
boot();
