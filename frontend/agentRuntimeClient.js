/**
 * agent_runtime — Admin client SDK (ES6, zero-dependency).
 *
 * Thin wrapper over the /admin API. Same-origin by default (the UI is served by the
 * agent_runtime FastAPI app), so the base is derived from where this page lives.
 * Methods return Promises of plain objects and reject with a typed RuntimeError.
 */

export class RuntimeError extends Error {
  constructor(status, detail) {
    super(`[${status}] ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    this.name = "RuntimeError";
    this.status = status;
    this.detail = detail;
  }
}

export class AgentRuntimeClient {
  constructor(baseUrl = "", { timeoutMs = 15000, fetch: fetchImpl } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
    this._fetch = fetchImpl || globalThis.fetch.bind(globalThis);
  }

  async _request(method, path, body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    let resp;
    try {
      resp = await this._fetch(this.baseUrl + path, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    if (resp.ok) {
      if (resp.status === 204) return null;
      const text = await resp.text();
      return text ? JSON.parse(text) : null;
    }
    let detail;
    try { detail = (await resp.json()).detail; }
    catch { detail = await resp.text().catch(() => resp.statusText); }
    throw new RuntimeError(resp.status, detail);
  }

  // health
  health() { return this._request("GET", "/health"); }

  // records
  listAgents() { return this._request("GET", "/admin/agents"); }
  listAgentsDetail() { return this._request("GET", "/admin/agents?detail=1"); }
  getAgent(uid) { return this._request("GET", `/admin/agents/${encodeURIComponent(uid)}`); }
  createAgent(record) { return this._request("POST", "/admin/agents", record); }
  updateAgent(uid, record) { return this._request("PUT", `/admin/agents/${encodeURIComponent(uid)}`, record); }
  deleteAgent(uid) { return this._request("DELETE", `/admin/agents/${encodeURIComponent(uid)}`); }
  validateAgent(record) { return this._request("POST", "/admin/agents/validate", record); }
  reload() { return this._request("POST", "/admin/reload"); }

  // observability + seam
  listRuns(agentUid, limit = 100) {
    const q = new URLSearchParams();
    if (agentUid) q.set("agent_uid", agentUid);
    q.set("limit", String(limit));
    return this._request("GET", `/admin/runs?${q.toString()}`);
  }
  consistency() { return this._request("GET", "/admin/consistency"); }
  listWhatsappTargets() { return this._request("GET", "/admin/channels/whatsapp/targets"); }
}
