// agent_runtime — Admin UI (vanilla ES6, class-based). Same-origin: the agent_runtime
// FastAPI app serves this page and the /admin API.

import { AgentRuntimeClient, RuntimeError } from "./agentRuntimeClient.js";

class AdminApp {
  constructor() {
    const base = window.location.pathname.replace(/\/index\.html$/, "").replace(/\/$/, "");
    this.client = new AgentRuntimeClient(base);
    this.$ = (s) => document.querySelector(s);
  }

  init() {
    this.applyTheme(localStorage.getItem("theme") || "light");
    this.$("#theme-toggle").addEventListener("click", () => this.toggleTheme());

    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", () => this.showTab(t.dataset.tab)));

    this.$("#refresh-agents").addEventListener("click", () => this.loadAgents());
    this.$("#agents-body").addEventListener("click", (e) => this.onRowAction(e));
    this.$("#refresh-consistency").addEventListener("click", () => this.loadConsistency());
    this.$("#refresh-runs").addEventListener("click", () => this.loadRuns());

    this.pollHealth();
    this.loadAgents();
    setInterval(() => this.pollHealth(), 10000);
  }

  // --- theme / tabs / health ----------------------------------------------

  applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
    const light = theme === "light";
    this.$("#theme-icon").textContent = light ? "🌙" : "☀️";
    this.$("#theme-label").textContent = light ? "Dark" : "Light";
  }
  toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme");
    this.applyTheme(cur === "light" ? "dark" : "light");
  }

  showTab(name) {
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) =>
      (p.hidden = p.dataset.panel !== name));
    if (name === "consistency") this.loadConsistency();
    if (name === "runs") this.loadRuns();
  }

  // The dot reflects service health; the text shows the active/inactive agent count
  // (kept current by loadAgents, which runs after every change).
  async pollHealth() {
    const dot = this.$("#health-dot");
    try {
      await this.client.health();
      dot.className = "dot ok";
    } catch {
      dot.className = "dot bad";
      this.$("#health-text").textContent = "unavailable";
    }
  }

  // --- agents table -------------------------------------------------------

  async loadAgents() {
    const body = this.$("#agents-body");
    try {
      // The deployed model is graph records (Agent Workflows) via /admin/projects; the legacy
      // flat /admin/agents store is (usually) empty. Show projects first, then any legacy agents.
      const [projRes, agentRes] = await Promise.all([
        this.client.listProjects().catch(() => ({ projects: [] })),
        this.client.listAgents().catch(() => ({ agents: [] })),
      ]);
      const projects = projRes.projects || [];
      const agents = agentRes.agents || [];
      const total = projects.length + agents.length;
      this.$("#agent-count").textContent = `(${total})`;
      const active = projects.filter((p) => p.enabled !== false).length
        + agents.filter((a) => a.enabled !== false).length;
      this.$("#health-text").textContent = `${active} active · ${total - active} inactive`;
      this._populateRunsAgents(agents.concat(projects.map((p) => ({ uid: p.uid, name: p.name }))));
      if (!total) {
        body.innerHTML = `<tr><td colspan="7" class="muted">no deployed workflows yet</td></tr>`;
        return;
      }
      body.innerHTML = projects.map((p) => this.projectRowHtml(p)).join("")
        + agents.map((a) => this.rowHtml(a)).join("");
    } catch (e) {
      body.innerHTML = `<tr><td colspan="7" class="form-msg bad">${this.esc(this.describe(e))}</td></tr>`;
    }
  }

  // A deployed Agent Workflow (GraphRecord from /admin/projects).
  projectRowHtml(p) {
    const inactive = p.enabled === false;
    const badge = inactive
      ? ` <span class="badge warn">disabled</span>`
      : ` <span class="badge">deployed</span>`;
    return `<tr${inactive ? ' class="row-inactive"' : ""}>
      <td><strong>${this.esc(p.name)}</strong>${badge}</td>
      <td><code>${this.esc(String(p.uid).slice(0, 8))}</code></td>
      <td>${this.esc(p.entry || "—")}</td>
      <td>v${this.esc(String(p.version))}</td>
      <td>${p.nodes} nodes · ${p.edges} edges</td>
      <td class="muted">workflow</td>
      <td class="row-actions">
        <button class="sm" data-act="runs" data-uid="${this.esc(p.uid)}">Runs</button>
        <button class="sm danger" data-act="undeploy" data-uid="${this.esc(p.uid)}" data-name="${this.esc(p.name)}">Undeploy</button>
      </td>
    </tr>`;
  }

  rowHtml(a) {
    const tools = a.tools_server ? `<code>${this.esc(a.tools_server)}</code> ·${a.tools_count}` : "—";
    const inactive = a.enabled === false;
    const badge = inactive ? ` <span class="badge warn">inactive</span>` : "";
    const toggle = inactive
      ? `<button class="sm" data-act="enable" data-uid="${this.esc(a.uid)}" data-name="${this.esc(a.name)}">Activate</button>`
      : `<button class="sm" data-act="disable" data-uid="${this.esc(a.uid)}" data-name="${this.esc(a.name)}">Deactivate</button>`;
    return `<tr${inactive ? ' class="row-inactive"' : ""}>
      <td><strong>${this.esc(a.name)}</strong>${badge}${a.description ? `<br><span class="muted small">${this.esc(a.description)}</span>` : ""}</td>
      <td><code>${this.esc(a.uid.slice(0, 8))}</code></td>
      <td>${this.esc(a.trigger_type)}</td>
      <td>${this.esc(a.persona)}</td>
      <td>${tools}</td>
      <td>${this.esc(a.delivery_channel)} → <code>${this.esc(a.delivery_target)}</code></td>
      <td class="row-actions">
        ${toggle}
        <button class="sm" data-act="runs" data-uid="${this.esc(a.uid)}">Runs</button>
        <button class="sm danger" data-act="delete" data-uid="${this.esc(a.uid)}" data-name="${this.esc(a.name)}">Delete</button>
      </td>
    </tr>`;
  }

  async onRowAction(event) {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const { act, uid, name } = btn.dataset;
    if (act === "enable" || act === "disable") {
      try {
        await (act === "enable" ? this.client.enableAgent(uid) : this.client.disableAgent(uid));
        this.toast(`${act === "enable" ? "Activated" : "Deactivated"} ${name}`, "ok");
        this.loadAgents();
      } catch (e) { this.toast(this.describe(e), "bad"); }
      return;
    }
    if (act === "runs") {
      this.showTab("runs");
      this.$("#runs-agent").value = uid;
      return this.loadRuns();
    }
    if (act === "undeploy") {
      if (!confirm(`Undeploy workflow "${name}"?\nRemoves the live record + firing binding.`)) return;
      try {
        await this.client.deleteProject(uid);
        this.toast(`Undeployed ${name}`, "ok");
        this.loadAgents();
      } catch (e) { this.toast(this.describe(e), "bad"); }
      return;
    }
    if (act === "delete") {
      if (!confirm(`Hard-delete agent "${name}"?\nThis removes the record permanently.`)) return;
      try {
        await this.client.deleteAgent(uid);
        this.toast(`Deleted ${name}`, "ok");
        this.loadAgents();
      } catch (e) { this.toast(this.describe(e), "bad"); }
    }
  }

  // --- consistency --------------------------------------------------------

  async loadConsistency() {
    const out = this.$("#consistency-out");
    out.innerHTML = `<p class="muted">loading…</p>`;
    try {
      const d = await this.client.consistency();
      const parts = [];
      if (!d.scheduler_ok) {
        parts.push(`<p class="badge bad">scheduler unreachable</p> <span class="muted small">${this.esc(d.scheduler_error || "")}</span>`);
      }
      parts.push(`<p><span class="badge ${d.dangling_count ? "bad" : "ok"}">${d.dangling_count} dangling job(s)</span>
        &nbsp; <span class="badge ${d.orphan_count ? "warn" : "ok"}">${d.orphan_count} orphan agent(s)</span></p>`);

      if (d.dangling.length) {
        parts.push(`<h3>Dangling jobs <span class="muted small">(point at a missing agent → dropped)</span></h3><table><thead><tr><th>Job</th><th>agent_uid</th><th>agent_name</th><th>Trigger</th></tr></thead><tbody>` +
          d.dangling.map((j) => `<tr><td><code>${this.esc(j.job_id || "")}</code></td><td><code>${this.esc(j.agent_uid || "—")}</code></td><td>${this.esc(j.agent_name || "—")}</td><td>${this.esc(j.trigger || "")}</td></tr>`).join("") +
          `</tbody></table>`);
      }

      parts.push(`<h3>Agents</h3><table><thead><tr><th>Name</th><th>Uid</th><th>State</th><th>Jobs</th></tr></thead><tbody>` +
        d.agents.map((a) => {
          const state = a.orphan ? `<span class="badge warn">orphan</span>` : `<span class="badge ok">linked</span>`;
          const jobs = a.jobs.length
            ? a.jobs.map((j) => `<code>${this.esc(j.job_id || "")}</code>${j.paused ? " (paused)" : ""}`).join(", ")
            : `<span class="muted">no job triggers this</span>`;
          return `<tr><td><strong>${this.esc(a.name)}</strong></td><td><code>${this.esc(a.uid.slice(0, 8))}</code></td><td>${state}</td><td>${jobs}</td></tr>`;
        }).join("") + `</tbody></table>`);

      out.innerHTML = parts.join("\n");
    } catch (e) {
      out.innerHTML = `<p class="form-msg bad">${this.esc(this.describe(e))}</p>`;
    }
  }

  // --- runs ---------------------------------------------------------------

  _populateRunsAgents(agents) {
    const sel = this.$("#runs-agent");
    const cur = sel.value;
    sel.innerHTML = `<option value="">all agents</option>` +
      agents.map((a) => `<option value="${this.esc(a.uid)}">${this.esc(a.name)}</option>`).join("");
    if (cur) sel.value = cur;
  }

  async loadRuns() {
    const out = this.$("#runs-out");
    out.innerHTML = `<p class="muted">loading…</p>`;
    try {
      const uid = this.$("#runs-agent").value;
      const { runs } = await this.client.listRuns(uid, 100);
      if (!runs.length) { out.innerHTML = `<p class="muted">no run events</p>`; return; }
      out.innerHTML = runs.map((r) => {
        const t = r.timestamp ? new Date(r.timestamp).toLocaleString() : "—";
        return `<div class="run-ev">
          <span class="muted">${this.esc(t)}</span>
          <span class="et">${this.esc(r.event_type)}</span>
          <span>${this.esc(r.agent_name || r.agent_uid || "")} <span class="muted small">${this.esc(this._runSummary(r))}</span></span>
        </div>`;
      }).join("");
    } catch (e) {
      out.innerHTML = `<p class="form-msg bad">${this.esc(this.describe(e))}</p>`;
    }
  }

  _runSummary(r) {
    const d = r.data || {};
    if (r.event_type === "tool.exec") return `${d.name || ""}`;
    if (r.event_type === "workflow.terminated") return `${d.reason || ""}`;
    if (r.event_type === "agent.result") return `→ ${d.channel || ""} ${d.delivery_id || ""}`;
    if (r.event_type === "agent.thought") return (d.thought || "").slice(0, 80);
    return `cid ${(r.cid || "").slice(0, 8)}`;
  }

  // --- helpers ------------------------------------------------------------

  describe(err) {
    if (err instanceof RuntimeError)
      return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
    return err.message || "request failed";
  }
  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  toast(text, kind) {
    const el = this.$("#toast");
    el.textContent = text; el.className = `toast ${kind || ""}`; el.hidden = false;
    clearTimeout(this._t); this._t = setTimeout(() => (el.hidden = true), 3000);
  }
}

new AdminApp().init();
