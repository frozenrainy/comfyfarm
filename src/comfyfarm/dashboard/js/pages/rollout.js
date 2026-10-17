// Rollout tab (docs/design/07): how each worker is launched (instance), what it should run (profile), and rolling a
// profile out one worker at a time — drain, restart, wait for the badge, verify, revert that worker on failure.
import { act, api, button, formDialog, h, namedRow, pill, setPill, setText, syncRows, table, when } from "../core.js";

const ACTIVE = ["running", "aborting", "interrupted"];
const PATHS = [["art", "Artifact store <ART>"], ["local", "Local root <LOCAL>"], ["models", "Models directory"],
               ["input", "Input directory"], ["output", "Output directory"], ["user", "User directory"], ["temp", "Temp directory"]];
const BACKEND_HINT = {
  manual: '{"timeout": 1800}  — an operator restarts the worker; the farm waits and watches',
  ssh: '{"host": "…", "user": "…", "key": "/path/on/router/id_ed25519", "port": 22}  — key limited to the agent\'s forced command',
  "http-agent": '{"url": "http://host:8399", "token": "…"}',
  "systemd-local": '{"unit": "comfyfarm@{instance}", "user": true}  — router and worker on the same host',
};
const head = (title, ...right) => h("div", { class: "section-head" }, h("h2", {}, title), ...right);
const words = (s) => s.split(/[\s,]+/).filter(Boolean);

export function rollout(root, { refresh }) {
  let profiles = [], insts = [], artifacts = null;
  const banner = h("div");
  const active = h("div");
  const instT = table([["Worker"], ["Instance"], ["Profile"], ["Badge"], ["Rev"], ["Running"], ["Restart via"], ["", "actions"]]);
  const profT = table([["Profile"], ["Core"], ["Venv"], ["Nodes"], ["Flags"], ["Assigned", "num"], ["Note"], ["", "actions"]]);
  const auditT = table([["When"], ["Who"], ["What"], ["Target"], ["Detail"]]);
  const history = h("div");

  const startBtn = button("Start rollout", async () => {
    const ready = insts.filter((i) => i.instance);
    if (!profiles.length || !ready.length) return act(async () => { throw new Error("needs a profile and at least one configured instance"); });
    const v = await formDialog("Start a rollout", [
      { name: "profile", label: "Roll out profile", type: "select", options: profiles.map((p) => ({ value: String(p.id), label: p.name })) },
      { name: "workers", label: "Workers (one at a time unless the window is larger)", type: "checkboxes",
        value: ready.filter((i) => !i.maintenance).map((i) => String(i.worker_id)),
        options: ready.map((i) => ({ value: String(i.worker_id), label: `${i.worker} — now ${i.profile ?? "unassigned"} (${i.badge})` })) },
      { name: "window", label: "Window (workers out of service at the same time)", type: "number", value: 1, min: 1 },
      { name: "drain", label: "Give up draining after (minutes, 0 = wait for the running job however long)", type: "number", value: 0, min: 0 },
      { name: "start", label: "Wait for the new version at most (minutes)", type: "number", value: 15, min: 1 },
      { name: "smoke", label: "Verify", type: "checkbox", text: "also run a tiny smoke workflow on each worker" },
    ], "Start");
    if (!v) return;
    const options = { start_timeout: (v.start || 15) * 60, smoke: v.smoke };
    if (v.drain) options.drain_timeout = v.drain * 60;
    const res = await act(() => api("POST", "/api/admin/rollouts", { profile_id: Number(v.profile), worker_ids: v.workers.map(Number),
                                                                   window: v.window || 1, options }), "Rollout started", refresh);
    if (res?.warnings?.length) alert("Started, with warnings:\n\n" + res.warnings.join("\n"));
  }, { cls: "primary" });

  root.append(
    h("section", {}, head("Rollout", startBtn), banner, active, history),
    h("section", {}, head("Instances"), h("p", { class: "muted small" },
      "How the launcher on each host starts the worker. Badge: match = the worker runs exactly its assigned render; " +
      "differs = it runs something else (not restarted yet, or a failed start)."), instT.el),
    h("section", {}, head("Profiles", button("New profile", () => editProfile(null), { cls: "primary" })),
      h("p", { class: "muted small" }, "A profile in use cannot be changed — copy it and roll the copy out."), profT.el),
    h("section", {}, head("Audit log"), auditT.el));

  // ---- instances ---------------------------------------------------------------------------------------------
  async function configure(i) {
    const cur = i.instance || { instance: i.worker.toLowerCase().replace(/[^a-z0-9._-]/g, "-"), hosts: [], listen: "0.0.0.0",
                                port: 8188, paths: {}, platform: "posix", role: "gpu", restart_backend: "manual", restart_config: {} };
    const v = await formDialog(`Instance of ${i.worker}`, [
      { name: "instance", label: "Instance name (the launcher's COMFY_LAUNCH_INSTANCE)", value: cur.instance },
      { name: "hosts", label: "Hosts allowed to fetch the render (addresses, comma-separated)", value: cur.hosts.join(", ") },
      { name: "listen", label: "ComfyUI --listen", value: cur.listen },
      { name: "port", label: "ComfyUI --port", type: "number", value: cur.port },
      { name: "platform", label: "Platform", type: "select", value: cur.platform,
        options: [{ value: "posix", label: "Linux / macOS" }, { value: "windows", label: "Windows" }] },
      { name: "role", label: "Role", type: "select", value: cur.role, options: [{ value: "gpu", label: "GPU worker" }, { value: "ui", label: "UI worker" }] },
      ...PATHS.map(([k, label]) => ({ name: `path_${k}`, label, value: cur.paths[k] ?? "" })),
      { name: "restart_backend", label: "Restart via", type: "select", value: cur.restart_backend,
        options: Object.keys(BACKEND_HINT).map((b) => ({ value: b, label: b })) },
      { name: "restart_config", label: "Restart configuration (JSON)", type: "textarea", rows: 3, value: JSON.stringify(cur.restart_config),
        hint: Object.entries(BACKEND_HINT).map(([k, t]) => `${k}: ${t}`).join("  ·  ") },
    ], "Save", { wide: true });
    if (!v) return;
    let cfg;
    try { cfg = v.restart_config.trim() ? JSON.parse(v.restart_config) : {}; } catch { return act(async () => { throw new Error("restart configuration is not JSON"); }); }
    const paths = Object.fromEntries(PATHS.map(([k]) => [k, v[`path_${k}`].trim()]));
    act(() => api("PUT", `/api/admin/instances/${i.worker_id}`, { instance: v.instance.trim(), hosts: words(v.hosts), listen: v.listen.trim(),
      port: v.port, platform: v.platform, role: v.role, paths, restart_backend: v.restart_backend, restart_config: cfg }), "Instance saved", refresh);
  }
  async function assign(i) {
    const v = await formDialog(`Assign a profile to ${i.worker}`, [
      { name: "profile", label: "Profile", type: "select", value: String(i.instance.profile_id ?? ""),
        options: profiles.map((p) => ({ value: String(p.id), label: p.name })),
        hint: "Without a restart: the badge shows 'differs' until the worker restarts. To change a running worker, start a rollout." }],
      "Assign");
    if (!v) return;
    const res = await act(() => api("POST", `/api/admin/instances/${i.worker_id}/assign`, { profile_id: Number(v.profile) }), "Assigned", refresh);
    if (res?.warnings?.length) alert(res.warnings.join("\n"));
  }
  async function showRender(i) {
    const r = await act(() => api("GET", `/api/admin/instances/${i.worker_id}/render`));
    if (r) formDialog(`Render of ${i.instance.instance} (rev ${r.rev})`, [{ name: "x", label: "", type: "pre", value: JSON.stringify(r, null, 2) }],
                      "", { wide: true, noSubmit: true });
  }
  const instCols = ["worker", "instance", "profile", "badge", "rev", "running", "via", "actions"];
  function buildInst() {
    const tr = namedRow(instCols, { actions: "actions" });
    tr.cfg = button("Configure", () => configure(tr._item));
    tr.asg = button("Assign", () => assign(tr._item));
    tr.ren = button("Render", () => showRender(tr._item));
    tr.c.actions.append(tr.cfg, tr.asg, tr.ren);
    return tr;
  }
  function updateInst(tr, i) {
    setText(tr.c.worker, i.worker);
    setText(tr.c.instance, i.instance ? i.instance.instance : "not configured");
    tr.c.instance.className = i.instance ? "" : "muted";
    setText(tr.c.profile, i.profile ?? "—");
    if (i.instance) setPill(tr.c.badge, i.badge); else setText(tr.c.badge, "");
    setText(tr.c.rev, i.rev ?? "");
    setText(tr.c.running, [i.comfyui ? `ComfyUI ${i.comfyui}` : null, i.backend].filter(Boolean).join(" · "));
    setText(tr.c.via, i.instance ? i.instance.restart_backend : "");
    tr.asg.hidden = tr.ren.hidden = !i.instance;
  }

  // ---- profiles ----------------------------------------------------------------------------------------------
  async function editProfile(p, copy = false) {
    const art = artifacts?.available ? artifacts.artifacts : null;
    const avail = (kind) => (art ? `published: ${art[kind].map((a) => a.name).join(", ") || "none"}` : "");
    const base = p || { name: "", core: "", venv: "", nodes: [], frontend: "", flags: [], env: {}, note: "" };
    const v = await formDialog(p && !copy ? `Edit ${p.name}` : copy ? `Copy of ${p.name}` : "New profile", [
      { name: "name", label: "Name", value: copy ? `${base.name}-2` : base.name },
      { name: "core", label: "Core artifact (ComfyUI release)", value: base.core, hint: avail("core") },
      { name: "venv", label: "Venv artifact", value: base.venv, hint: avail("venv") },
      { name: "nodes", label: "Node artifacts (comma-separated, load order)", value: base.nodes.join(", "), hint: avail("nodes") },
      { name: "frontend", label: "Frontend artifact (optional)", value: base.frontend ?? "", hint: avail("frontend") },
      { name: "flags", label: "Extra ComfyUI flags", type: "textarea", rows: 2, value: base.flags.join(" "),
        hint: "e.g. --preview-method auto --reserve-vram 1 (directories and ports come from the instance)" },
      { name: "env", label: "Environment (KEY=VALUE per line)", type: "textarea", rows: 3,
        value: Object.entries(base.env || {}).map(([k, x]) => `${k}=${x}`).join("\n"), hint: "only PYTORCH_*, HIP_*, CUDA_*, … keys the launcher allows" },
      { name: "note", label: "Note", value: base.note ?? "" },
    ], p && !copy ? "Save" : "Create", { wide: true });
    if (!v) return;
    const env = Object.fromEntries(v.env.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => [l.split("=")[0], l.slice(l.indexOf("=") + 1)]));
    const body = { name: v.name.trim(), core: v.core.trim(), venv: v.venv.trim(), nodes: words(v.nodes), frontend: v.frontend.trim() || null,
                   flags: v.flags.split(/\s+/).filter(Boolean), env, note: v.note || null };
    if (p && !copy) act(() => api("PATCH", `/api/admin/profiles/${p.id}`, body), "Profile saved", refresh);
    else act(() => api("POST", "/api/admin/profiles", body), "Profile created", refresh);
  }
  const profCols = ["name", "core", "venv", "nodes", "flags", "assigned", "note", "actions"];
  function buildProf() {
    const tr = namedRow(profCols, { assigned: "num", actions: "actions" });
    tr.edit = button("Edit", () => editProfile(tr._item));
    tr.c.actions.append(button("Copy", () => editProfile(tr._item, true)), tr.edit,
      button("Delete", () => { if (confirm(`Delete profile ${tr._item.name}?`)) act(() => api("DELETE", `/api/admin/profiles/${tr._item.id}`), "Deleted", refresh); },
             { cls: "danger" }));
    return tr;
  }
  function updateProf(tr, p) {
    setText(tr.c.name, p.name); setText(tr.c.core, p.core); setText(tr.c.venv, p.venv);
    setText(tr.c.nodes, p.nodes.join(", ") || "—"); setText(tr.c.flags, p.flags.join(" ") || "—");
    setText(tr.c.assigned, p.assigned); setText(tr.c.note, p.note ?? "");
    tr.edit.title = p.assigned ? "in use: only the note can change" : "";
  }

  // ---- rollouts ----------------------------------------------------------------------------------------------
  function stepsTable(ro) {
    const t = table([["#", "num"], ["Worker"], ["From → to"], ["Step"], ["Detail"], ["Error"], ["Started"], ["Took", "num"]]);
    for (const s of ro.steps) {
      const took = s.started_at ? ((s.finished_at ? Date.parse(s.finished_at) : Date.now()) - Date.parse(s.started_at)) / 1000 : null;
      t.tbody.append(h("tr", {}, h("td", { class: "num" }, String(s.position + 1)), h("td", {}, s.worker),
        h("td", {}, `${s.from ?? "—"} → ${s.to}`), h("td", {}, pill(s.status)), h("td", {}, s.detail ?? ""),
        h("td", { class: "err" }, s.error ?? ""), h("td", {}, s.started_at ? when(s.started_at) : ""),
        h("td", { class: "num" }, took == null ? "" : `${Math.round(took)}s`)));
    }
    return t.el;
  }
  function renderActive(list) {
    const cur = list.find((r) => ACTIVE.includes(r.status));
    banner.replaceChildren(...(cur ? cur.steps.filter((s) => s.backend === "manual" && s.status === "restarting").map((s) =>
      h("div", { class: "callout" }, h("strong", {}, "Your turn: "),
        `restart ${s.instance} on its host now (e.g. systemctl restart comfyfarm@${s.instance}). The farm watches it go down and come back.`)) : []));
    if (!cur) { active.replaceChildren(h("p", { class: "muted" }, "No rollout running.")); return; }
    const buttons = [];
    if (cur.status === "running") buttons.push(button("Abort", () => {
      if (confirm("Abort? Workers still draining go back to work; restarts already under way finish.")) act(() => api("POST", `/api/admin/rollouts/${cur.id}/abort`), "Aborting", refresh);
    }, { cls: "danger" }));
    if (cur.status === "interrupted") buttons.push(
      button("Resume", () => act(() => api("POST", `/api/admin/rollouts/${cur.id}/resume`), "Resumed", refresh), { cls: "primary" }),
      button("Abort", () => act(() => api("POST", `/api/admin/rollouts/${cur.id}/abort`), "Aborted", refresh), { cls: "danger" }));
    active.replaceChildren(
      h("div", { class: "section-head" }, h("h3", {}, `#${cur.id} → ${cur.profile}`), pill(cur.status),
        h("span", { class: "muted" }, `by ${cur.created_by}, ${when(cur.created_at)} · window ${cur.window}`), ...buttons),
      cur.status === "interrupted" ? h("div", { class: "callout" }, h("strong", {}, "Interrupted: "),
        "the router restarted during this rollout. Its workers stay in maintenance until you resume or abort.") : "",
      stepsTable(cur));
  }
  function renderHistory(list) {
    const done = list.filter((r) => !ACTIVE.includes(r.status)).slice(0, 8);
    history.replaceChildren(...(done.length ? [h("h3", {}, "Recent rollouts"), ...done.map((r) => h("details", {},
      h("summary", {}, pill(r.status), ` #${r.id} → ${r.profile} · ${r.steps.filter((s) => s.status === "done").length}/${r.steps.length} done · ${when(r.finished_at || r.created_at)} · ${r.created_by}`),
      stepsTable(r)))] : []));
  }

  return {
    poll: 2000,
    async refresh() {
      const [i, p, r, a] = await Promise.all([api("GET", "/api/admin/instances"), api("GET", "/api/admin/profiles"),
        api("GET", "/api/admin/rollouts?limit=10"), api("GET", "/api/admin/audit?limit=30")]);
      insts = i; profiles = p;
      if (artifacts === null) artifacts = await api("GET", "/api/admin/artifacts");
      syncRows(instT.tbody, i, (x) => x.worker_id, buildInst, updateInst);
      syncRows(profT.tbody, p, (x) => x.id, buildProf, updateProf);
      renderActive(r);
      const sig = JSON.stringify(r.filter((x) => !ACTIVE.includes(x.status)).map((x) => [x.id, x.status]));
      if (history.dataset.v !== sig) { history.dataset.v = sig; renderHistory(r); }
      auditT.tbody.replaceChildren(...a.map((e) => h("tr", {}, h("td", {}, when(e.at)), h("td", {}, e.actor), h("td", {}, e.action),
        h("td", {}, e.target ?? ""), h("td", { class: "muted small" }, Object.keys(e.detail || {}).length ? JSON.stringify(e.detail).slice(0, 160) : ""))));
    },
  };
}
