// agent_runtime — Admin UI (vanilla ES6, class-based). Same-origin: the agent_runtime
// FastAPI app serves this page and the /admin API.

import { AgentRuntimeClient, RuntimeError } from "./agentRuntimeClient.js";

class AdminApp {
  constructor() {
    const base = window.location.pathname.replace(/\/index\.html$/, "").replace(/\/$/, "");
    this.client = new AgentRuntimeClient(base);
    this.$ = (s) => document.querySelector(s);
    this.editingUid = null;
  }

  init() {
    this.applyTheme(localStorage.getItem("theme") || "light");
    this.$("#theme-toggle").addEventListener("click", () => this.toggleTheme());

    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", () => this.showTab(t.dataset.tab)));

    this.form = this.$("#agent-form");
    this.form.addEventListener("submit", (e) => this.onSubmit(e));
    this.$("#validate-btn").addEventListener("click", () => this.onValidate());
    this.$("#cancel-edit").addEventListener("click", () => this.exitEdit());
    this.$("#refresh-agents").addEventListener("click", () => this.loadAgents());
    this.$("#agents-body").addEventListener("click", (e) => this.onRowAction(e));
    this.$("#refresh-consistency").addEventListener("click", () => this.loadConsistency());
    this.$("#refresh-runs").addEventListener("click", () => this.loadRuns());
    this.form.elements["delivery_channel"].addEventListener("change", () => this.onDeliveryChannelChange());
    this.$("#wa-target-select").addEventListener("change", () => this.onWaSelect());
    this.onDeliveryChannelChange(); // channel defaults to whatsapp -> show + load the picker

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

  async pollHealth() {
    const dot = this.$("#health-dot"), text = this.$("#health-text");
    try {
      const h = await this.client.health();
      dot.className = "dot ok";
      text.textContent = `ok · ${h.version}`;
    } catch {
      dot.className = "dot bad"; text.textContent = "unavailable";
    }
  }

  // --- form: collect / validate / submit ----------------------------------

  _get(name) { const el = this.form.elements[name]; return el ? el.value.trim() : ""; }

  collectRecord() {
    const g = (n) => this._get(n);
    const rec = {
      version: g("version") || "0.1",
      name: g("name"),
      enabled: this.form.elements["active"].checked,
      trigger: { type: g("trigger_type") },
      brain: { persona: g("persona") },
      input: { template: g("input_template"), vars: {} },
      delivery: { channel: g("delivery_channel"), target: g("delivery_target") },
    };
    if (!rec.delivery.target) throw new Error("Delivery target required");
    const desc = g("description"); if (desc) rec.description = desc;

    const llm = {};
    if (g("temperature")) llm.temperature = Number(g("temperature"));
    if (g("max_tokens")) llm.max_tokens = Number(g("max_tokens"));
    if (Object.keys(llm).length) rec.brain.llm = llm;

    const server = g("tools_server");
    if (server) {
      rec.tools = { server, allow: g("tools_allow").split(/[\s,]+/).filter(Boolean) };
      if (g("tools_max_rounds")) rec.tools.max_rounds = Number(g("tools_max_rounds"));
    }

    const varsRaw = g("input_vars");
    if (varsRaw) {
      let parsed;
      try { parsed = JSON.parse(varsRaw); }
      catch { throw new Error("Input vars must be valid JSON"); }
      rec.input.vars = parsed;
    }
    if (this.editingUid) rec.uid = this.editingUid;
    return rec;
  }

  msg(text, kind) {
    const m = this.$("#form-msg");
    m.className = `form-msg ${kind || ""}`;
    m.textContent = text;
  }

  async onValidate() {
    let rec;
    try { rec = this.collectRecord(); }
    catch (e) { return this.msg(e.message, "bad"); }
    try {
      const r = await this.client.validateAgent(rec);
      if (r.ok) this.msg("✓ valid record", "ok");
      else this.msg("Invalid:\n- " + r.errors.map((e) => `${e.loc}: ${e.msg}`).join("\n- "), "bad");
    } catch (e) { this.msg(this.describe(e), "bad"); }
  }

  async onSubmit(event) {
    event.preventDefault();
    let rec;
    try { rec = this.collectRecord(); }
    catch (e) { return this.msg(e.message, "bad"); }
    try {
      if (this.editingUid) {
        const r = await this.client.updateAgent(this.editingUid, rec);
        this.toast(`Saved ${r.name}`, "ok");
        this.exitEdit();
      } else {
        const r = await this.client.createAgent(rec);
        this.toast(`Created ${r.name} (${r.uid.slice(0, 8)})`, "ok");
        this.form.reset();
      }
      this.loadAgents();
    } catch (e) { this.msg(this.describe(e), "bad"); }
  }

  // --- edit mode ----------------------------------------------------------

  // Fill every form field from a record (shared by Edit and Duplicate).
  _fillForm(rec) {
    const set = (n, v) => { const el = this.form.elements[n]; if (el) el.value = v ?? ""; };
    set("name", rec.name); set("uid", rec.uid); set("version", rec.version || "0.1");
    this.form.elements["active"].checked = rec.enabled !== false;
    set("description", rec.description);
    set("trigger_type", rec.trigger?.type || "schedule");
    set("persona", rec.brain?.persona);
    set("temperature", rec.brain?.llm?.temperature);
    set("max_tokens", rec.brain?.llm?.max_tokens);
    set("tools_server", rec.tools?.server);
    set("tools_allow", (rec.tools?.allow || []).join(" "));
    set("tools_max_rounds", rec.tools?.max_rounds);
    set("input_template", rec.input?.template);
    set("input_vars",
      rec.input?.vars && Object.keys(rec.input.vars).length ? JSON.stringify(rec.input.vars, null, 2) : "");
    set("delivery_channel", rec.delivery?.channel || "whatsapp");
    set("delivery_target", rec.delivery?.target);
    this.onDeliveryChannelChange(rec.delivery?.target); // show + preselect the picker
  }

  async enterEdit(uid) {
    let rec;
    try { rec = await this.client.getAgent(uid); }
    catch (e) { return this.toast(this.describe(e), "bad"); }
    this.editingUid = uid;
    this._fillForm(rec);
    this.$("#uid-field").hidden = false;
    this.$("#form-title").textContent = `Edit agent: ${rec.name}`;
    this.$("#submit-btn").textContent = "Save";
    this.$("#cancel-edit").hidden = false;
    this.msg("", "");
    this.$("#agent-form").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Duplicate: load an existing record into the form in CREATE mode (no uid -> the
  // server assigns a fresh one on save). Only the name is pre-changed.
  async enterDuplicate(uid) {
    let rec;
    try { rec = await this.client.getAgent(uid); }
    catch (e) { return this.toast(this.describe(e), "bad"); }
    this.editingUid = null;                    // create a NEW record on save
    this._fillForm(rec);
    this.form.elements["name"].value = `Copy of ${rec.name}`;
    this.$("#uid-field").hidden = true;        // create mode shows no uid
    this.$("#form-title").textContent = `Create agent (copy of ${rec.name})`;
    this.$("#submit-btn").textContent = "Create";
    this.$("#cancel-edit").hidden = false;
    this.msg("Duplicated — change what you need, then Create.", "ok");
    this.$("#agent-form").scrollIntoView({ behavior: "smooth", block: "start" });
    this.form.elements["name"].focus();
    this.form.elements["name"].select();
  }

  exitEdit() {
    this.editingUid = null;
    this.form.reset();
    this.$("#uid-field").hidden = true;
    this.$("#form-title").textContent = "Create agent";
    this.$("#submit-btn").textContent = "Create";
    this.$("#cancel-edit").hidden = true;
    this.msg("", "");
    this.onDeliveryChannelChange(); // reset() restores channel=whatsapp -> refresh picker
  }

  // --- delivery target dropdown (whatsapp) --------------------------------

  // whatsapp -> show the chat picker (and the text box only when "Type an id…" is
  // chosen). bus/tts -> no picker, just the text box.
  onDeliveryChannelChange(selectedId) {
    const sel = this.$("#wa-target-select");
    const inp = this.form.elements["delivery_target"];
    const isWa = this.form.elements["delivery_channel"].value === "whatsapp";
    sel.hidden = !isWa;
    if (isWa) {
      this.loadWaTargets(selectedId ?? inp.value); // manages the text box visibility
    } else {
      inp.hidden = false; // bus/tts: free text only
    }
  }

  // A real chat -> store its id and hide the box; "Type an id…" -> reveal the box;
  // placeholder -> hide + clear.
  onWaSelect() {
    const v = this.$("#wa-target-select").value;
    const inp = this.form.elements["delivery_target"];
    if (v === "__custom__") {
      inp.hidden = false; inp.focus();
    } else if (v) {
      inp.value = v; inp.hidden = true;
    } else {
      inp.value = ""; inp.hidden = true;
    }
  }

  async loadWaTargets(selectedId) {
    const sel = this.$("#wa-target-select");
    const inp = this.form.elements["delivery_target"];
    sel.innerHTML = `<option value="">— loading chats… —</option>`;
    inp.hidden = true;
    const fallbackToTyping = (label) => {
      sel.innerHTML = `<option value="__custom__">${label}</option>`;
      sel.value = "__custom__";
      inp.hidden = false;
      if (selectedId) inp.value = selectedId;
    };
    let data;
    try {
      data = await this.client.listWhatsappTargets();
    } catch (e) {
      return fallbackToTyping("Type an id… (couldn't load chats)");
    }
    if (!data.bridge_ok) {
      return fallbackToTyping("Type an id… (chat list unavailable)");
    }
    const groups = data.targets.filter((t) => t.kind === "group");
    const contacts = data.targets.filter((t) => t.kind === "contact");
    const optList = (arr) => arr.map((t) =>
      `<option value="${this.esc(t.id)}">${this.esc(t.name)}</option>`).join("");
    let html = `<option value="">— select a WhatsApp chat —</option>`;
    html += `<option value="__custom__">Type an id…</option>`;
    if (groups.length) html += `<optgroup label="Groups">${optList(groups)}</optgroup>`;
    if (contacts.length) html += `<optgroup label="Contacts">${optList(contacts)}</optgroup>`;
    sel.innerHTML = html;
    // Preselect from the current target: a known chat -> select it (box hidden);
    // anything else -> "Type an id…" with the box shown holding that value.
    if (selectedId && data.targets.some((t) => t.id === selectedId)) {
      sel.value = selectedId; inp.value = selectedId; inp.hidden = true;
    } else if (selectedId) {
      sel.value = "__custom__"; inp.value = selectedId; inp.hidden = false;
    } else {
      sel.value = ""; inp.hidden = true;
    }
  }

  // --- agents table -------------------------------------------------------

  async loadAgents() {
    const body = this.$("#agents-body");
    try {
      const { agents } = await this.client.listAgents();
      this.$("#agent-count").textContent = `(${agents.length})`;
      this._populateRunsAgents(agents);
      if (!agents.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">no agents yet</td></tr>`;
        return;
      }
      body.innerHTML = agents.map((a) => this.rowHtml(a)).join("");
    } catch (e) {
      body.innerHTML = `<tr><td colspan="7" class="form-msg bad">${this.esc(this.describe(e))}</td></tr>`;
    }
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
        <button class="sm" data-act="edit" data-uid="${this.esc(a.uid)}">Edit</button>
        <button class="sm" data-act="duplicate" data-uid="${this.esc(a.uid)}">Copy</button>
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
    if (act === "edit") return this.enterEdit(uid);
    if (act === "duplicate") return this.enterDuplicate(uid);
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
    if (act === "delete") {
      if (!confirm(`Hard-delete agent "${name}"?\nThis removes the record permanently.`)) return;
      try {
        await this.client.deleteAgent(uid);
        if (this.editingUid === uid) this.exitEdit();
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
