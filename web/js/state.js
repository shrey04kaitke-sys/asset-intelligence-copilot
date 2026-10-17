// Tiny shared state: who is signed in (a picker, not authentication) and a hash router.
export const state = { userId: null, users: [], health: null, listeners: new Set() };

export function setUser(id) {
  state.userId = id;
  try { localStorage.setItem("copilot.user", id); } catch (_) { /* storage unavailable (private mode): fine */ }
  state.listeners.forEach((f) => f());
}
export function storedUser() { try { return localStorage.getItem("copilot.user"); } catch (_) { return null; } }
export const currentUser = () => state.users.find((u) => u.user_id === state.userId) || null;

export function parseHash() {
  const raw = (location.hash || "#/overview").slice(1);
  const [path, query] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: Object.fromEntries(new URLSearchParams(query || "")) };
}
export function link(route, params) {
  const q = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v !== null && v !== undefined && v !== "")).toString();
  return `#/${route}${q ? "?" + q : ""}`;
}
