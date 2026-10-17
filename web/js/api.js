// Backend client. Every failure becomes an ApiError with the server's own message; nothing is swallowed or faked.

export class ApiError extends Error {
  constructor(status, code, message, retryAfter) {
    super(message);
    this.status = status; this.code = code; this.retryAfter = retryAfter || null;
  }
}

function describe(body, status) {
  if (body && body.error && body.error.message) return [body.error.code, body.error.message];
  if (body && typeof body.detail === "string") return [`http_${status}`, body.detail];
  if (body && Array.isArray(body.detail)) return ["invalid_request", body.detail.map((d) => `${(d.loc || []).slice(1).join(".") || "input"}: ${d.msg}`).join("; ")];
  return [`http_${status}`, `The server answered with status ${status}.`];
}

async function request(method, path, { params, body, signal } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, String(v));
  let resp;
  try {
    resp = await fetch(url, { method, signal, headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, "network", "Could not reach the server. Is it running?");
  }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON body */ }
  if (!resp.ok) {
    const [code, message] = describe(data, resp.status);
    throw Object.assign(new ApiError(resp.status, code, message, resp.headers.get("retry-after")), { body: data });
  }
  return data;
}

export const api = {
  health: () => request("GET", "/health"),
  users: () => request("GET", "/users"),
  examples: () => request("GET", "/examples"),
  fleet: (p) => request("GET", "/fleet", { params: p }),
  assets: (p) => request("GET", "/assets", { params: p }),
  asset: (id, user) => request("GET", `/assets/${encodeURIComponent(id)}`, { params: { user_id: user } }),
  evidence: (ref, user) => request("GET", "/evidence", { params: { ref, user_id: user } }),
  audit: (p) => request("GET", "/audit", { params: p }),
  knowledge: (user) => request("GET", "/knowledge", { params: { user_id: user } }),
  conversations: (user) => request("GET", "/conversations", { params: { user_id: user } }),
  messages: (id, user) => request("GET", `/conversations/${encodeURIComponent(id)}/messages`, { params: { user_id: user } }),
  confirm: (body) => request("POST", "/confirm", { body }),
  postCorrection: (body) => request("POST", "/feedback/correction", { body }),
  postClaim: (body) => request("POST", "/feedback/claim", { body }),
  postRule: (body) => request("POST", "/feedback/rule", { body }),
  withdraw: (body) => request("POST", "/feedback/withdraw", { body }),
  // /ask returns a structured body for failures too (error code, trace, retry hint); surface it instead of hiding it
  async ask(body, signal) {
    try { return await request("POST", "/ask", { body, signal }); }
    catch (e) {
      if (e instanceof ApiError && e.body && typeof e.body === "object" && "ok" in e.body && e.body.error) return { ...e.body, _status: e.status, _retryAfter: e.retryAfter };
      throw e;
    }
  },
};
