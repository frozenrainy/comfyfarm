// Rollout tab (docs/design/07): how each worker is launched (instance), what it should run (profile), and rolling a
// profile out one worker at a time — drain, restart, wait for the badge, verify, revert that worker on failure.
import { act, api, button, formDialog, h, namedRow, pill, setPill, setText, syncRows, t, table, toast, when } from "../core.js";
import { checkHost } from "./setup.js";

const ACTIVE = ["running", "aborting", "interrupted"];
const PATHS = [["art", "Artifact store <ART>"], ["local", "Local root <LOCAL>"], ["models", "Models directory"],
               ["input", "Input directory"], ["output", "Output directory"], ["user", "User directory"], ["temp", "Temp directory"]];   // [key, English label]
const BACKEND_HINT = {                    // [example configuration, English explanation]
  manual: ['{"timeout": 1800}', "an operator restarts the worker; the farm waits and watches"],
  ssh: ['{"host": "…", "user": "…", "key": "<KEYS_DIR>/id_ed25519", "port": 22}',
        "the key file lies in the router's COMFYFARM_KEYS_DIR and is limited to the agent's forced command"],
  "http-agent": ['{"url": "http://host:8399", "token_file": "<KEYS_DIR>/agent.token"}',
                 "or \"token_env\": \"COMFYFARM_AGENT_TOKEN_…\"; the token value itself is never stored"],
  "systemd-local": ['{"unit": "comfyfarm@{instance}", "user": true}', "router and worker on the same host"],
};
// manual and ssh cover a normal site; the other two exist for special cases (no ssh, or router and worker on one host)
const ADVANCED = ["http-agent", "systemd-local"];
const head = (title, ...right) => h("div", { class: "section-head" }, h("h2", {}, title), ...right);
const words = (s) => s.split(/[\s,]+/).filter(Boolean);

export function rollout(root, { refresh }) {
  let profiles = [], insts = [], artifacts = null;
  const banner = h("div");
  const active = h("div");
  const instT = table([[t("Worker")], [t("Instance")], [t("Profile")], [t("Badge")], [t("Rev")], [t("Running")], [t("Restart via")], ["", "actions"]]);
  const profT = table([[t("Profile")], [t("Core")], [t("Venv")], [t("Nodes")], [t("Flags")], [t(t("Assigned")), "num"], [t("Note")], ["", "actions"]]);
  const auditT = table([[t("When")], [t("Who")], [t("What")], [t("Target")], [t("Detail")]]);
  const history = h("div");

  const startBtn = button(t("Start rollout"), async () => {
    const ready = insts.filter((i) => i.instance);
    if (!profiles.length || !ready.length) return act(async () => { throw new Error(t("needs a profile and at least one configured instance")); });
    const v = await formDialog(t("Start a rollout"), [
      { name: "profile", label: t("Roll out profile"), type: "select", options: profiles.map((p) => ({ value: String(p.id), label: p.name })) },
      { name: "workers", label: t("Workers (one at a time unless the window is larger)"), type: "checkboxes",
        value: ready.filter((i) => !i.maintenance).map((i) => String(i.worker_id)),
        options: ready.map((i) => ({ value: String(i.worker_id), label: t("{worker} — now {profile} ({badge})", { worker: i.worker, profile: i.profile ?? t("unassigned"), badge: t(i.badge) }) })) },
      { name: "window", label: t("Window (workers out of service at the same time)"), type: "number", value: 1, min: 1 },
      { name: "drain", label: t("Give up draining after (minutes, 0 = wait for the running job however long)"), type: "number", value: 0, min: 0 },
      { name: "start", label: t("Wait for the new version at most (minutes)"), type: "number", value: 15, min: 1 },
      { name: "smoke", label: t("Verify"), type: "checkbox", text: t("also run a tiny smoke workflow on each worker") },
    ], t("Start"));
    if (!v) return;
    const options = { start_timeout: (v.start || 15) * 60, smoke: v.smoke };
    if (v.drain) options.drain_timeout = v.drain * 60;
    const res = await act(() => api("POST", "/api/admin/rollouts", { profile_id: Number(v.profile), worker_ids: v.workers.map(Number),
                                                                   window: v.window || 1, options }), t("Rollout started"), refresh);
    if (res?.warnings?.length) alert(t("Started, with warnings:") + "\n\n" + res.warnings.join("\n"));
  }, { cls: "primary" });

  root.append(
    h("section", {}, head(t("Rollout"), startBtn), banner, active, history),
    h("section", {}, head(t("Instances")), h("p", { class: "muted small" },
      t("How the launcher on each host starts the worker. Badge: match = the worker runs exactly its assigned render; " +
      "differs = it runs something else (not restarted yet, or a failed start).")), instT.el),
    h("section", {}, head(t("Profiles"), button(t("From a recipe"), () => fromRecipe()),
                          button(t("New profile"), () => editProfile(null), { cls: "primary" })),
      h("p", { class: "muted small" }, t("A profile in use cannot be changed — copy it and roll the copy out.")), profT.el),
    h("section", {}, head(t("Audit log")), auditT.el));

  // ---- instances ---------------------------------------------------------------------------------------------
  async function configure(i) {
    const cur = i.instance || { instance: i.worker.toLowerCase().replace(/[^a-z0-9._-]/g, "-"), hosts: [], listen: "0.0.0.0",
                                port: 8188, paths: {}, platform: "posix", role: "gpu", restart_backend: "manual", restart_config: {} };
    const v = await formDialog(t("Instance of {worker}", i), [
      { name: "instance", label: t("Instance name (the launcher's COMFY_LAUNCH_INSTANCE)"), value: cur.instance },
      { name: "hosts", label: t("Hosts allowed to fetch the render (addresses, comma-separated)"), value: cur.hosts.join(", ") },
      { name: "listen", label: "ComfyUI --listen", value: cur.listen },
      { name: "port", label: "ComfyUI --port", type: "number", value: cur.port },
      { name: "platform", label: t("Platform"), type: "select", value: cur.platform,
        options: [{ value: "posix", label: t("Linux / macOS") }, { value: "windows", label: t("Windows") }] },
      { name: "role", label: t("Role"), type: "select", value: cur.role, options: [{ value: "gpu", label: t("GPU worker") }, { value: "ui", label: t("UI worker") }] },
      ...PATHS.map(([k, label]) => ({ name: `path_${k}`, label: t(label), value: cur.paths[k] ?? "" })),
      { name: "restart_backend", label: t("Restart via"), type: "select", value: cur.restart_backend,
        options: Object.keys(BACKEND_HINT).map((b) => ({ value: b, label: ADVANCED.includes(b) ? t("{name} (advanced)", { name: b }) : b })) },
      { name: "restart_config", label: t("Restart configuration (JSON)"), type: "textarea", rows: 3, value: JSON.stringify(cur.restart_config),
        hint: Object.entries(BACKEND_HINT).filter(([k]) => !ADVANCED.includes(k) || k === cur.restart_backend)
          .map(([k, [ex, why]]) => `${k}: ${ex}  — ${t(why)}`).join("  ·  ") },
    ], t("Save"), { wide: true });
    if (!v) return;
    let cfg;
    try { cfg = v.restart_config.trim() ? JSON.parse(v.restart_config) : {}; } catch { return act(async () => { throw new Error(t("restart configuration is not JSON")); }); }
    const paths = Object.fromEntries(PATHS.map(([k]) => [k, v[`path_${k}`].trim()]));
    act(() => api("PUT", `/api/admin/instances/${i.worker_id}`, { instance: v.instance.trim(), hosts: words(v.hosts), listen: v.listen.trim(),
      port: v.port, platform: v.platform, role: v.role, paths, restart_backend: v.restart_backend, restart_config: cfg }), t("Instance saved"), refresh);
  }
  async function assign(i) {
    const v = await formDialog(t("Assign a profile to {worker}", i), [
      { name: "profile", label: t("Profile"), type: "select", value: String(i.instance.profile_id ?? ""),
        options: profiles.map((p) => ({ value: String(p.id), label: p.name })),
        hint: t("Without a restart: the badge shows 'differs' until the worker restarts. To change a running worker, start a rollout.") }],
      t("Assign"));
    if (!v) return;
    const res = await act(() => api("POST", `/api/admin/instances/${i.worker_id}/assign`, { profile_id: Number(v.profile) }), t(t("Assigned")), refresh);
    if (res?.warnings?.length) alert(res.warnings.join("\n"));
  }
  async function showRender(i) {
    const r = await act(() => api("GET", `/api/admin/instances/${i.worker_id}/render`));
    if (r) formDialog(t("Render of {instance} (rev {rev})", { instance: i.instance.instance, rev: r.rev }), [{ name: "x", label: "", type: "pre", value: JSON.stringify(r, null, 2) }],
                      "", { wide: true, noSubmit: true });
  }
  const instCols = ["worker", "instance", "profile", "badge", "rev", "running", "via", "actions"];
  function buildInst() {
    const tr = namedRow(instCols, { actions: "actions" });
    tr.cfg = button(t("Configure"), () => configure(tr._item));
    tr.asg = button(t("Assign"), () => assign(tr._item));
    tr.ren = button(t("Render"), () => showRender(tr._item));
    tr.chk = button(t("Check host"), () => checkHost(tr._item.worker_id, tr._item.worker),
                    { title: t("Read-only check of the host through its agent: driver, groups, directories, venvs, clock, router") });
    tr.c.actions.append(tr.cfg, tr.asg, tr.ren, tr.chk);
    return tr;
  }
  function updateInst(tr, i) {
    setText(tr.c.worker, i.worker);
    setText(tr.c.instance, i.instance ? i.instance.instance : t("not configured"));
    tr.c.instance.className = i.instance ? "" : "muted";
    setText(tr.c.profile, i.profile ?? "—");
    if (i.instance) setPill(tr.c.badge, i.badge); else setText(tr.c.badge, "");
    setText(tr.c.rev, i.rev ?? "");
    setText(tr.c.running, [i.comfyui ? `ComfyUI ${i.comfyui}` : null, i.backend].filter(Boolean).join(" · "));
    setText(tr.c.via, i.instance ? i.instance.restart_backend : "");
    tr.asg.hidden = tr.ren.hidden = !i.instance;
    tr.chk.hidden = !i.instance || !["ssh", "http-agent"].includes(i.instance.restart_backend);
  }

  // ---- profiles ----------------------------------------------------------------------------------------------
  async function editProfile(p, copy = false) {
    const art = artifacts?.available ? artifacts.artifacts : null;
    const avail = (kind) => (art ? t("published: {names}", { names: art[kind].map((a) => a.name).join(", ") || t("none") }) : "");
    const base = p || { name: "", core: "", venv: "", nodes: [], frontend: "", flags: [], env: {}, note: "" };
    const v = await formDialog(p && !copy ? t("Edit {name}", p) : copy ? t("Copy of {name}", p) : t("New profile"), [
      { name: "name", label: t("Name"), value: copy ? `${base.name}-2` : base.name },
      { name: "core", label: t("Core artifact (ComfyUI release)"), value: base.core, hint: avail("core") },
      { name: "venv", label: t("Venv artifact"), value: base.venv, hint: avail("venv") },
      { name: "nodes", label: t("Node artifacts (comma-separated, load order)"), value: base.nodes.join(", "), hint: avail("nodes") },
      { name: "frontend", label: t("Frontend artifact (optional)"), value: base.frontend ?? "", hint: avail("frontend") },
      { name: "flags", label: t("Extra ComfyUI flags"), type: "textarea", rows: 2, value: base.flags.join(" "),
        hint: t("e.g. --preview-method auto --reserve-vram 1 (directories and ports come from the instance)") },
      { name: "env", label: t("Environment (KEY=VALUE per line)"), type: "textarea", rows: 3,
        value: Object.entries(base.env || {}).map(([k, x]) => `${k}=${x}`).join("\n"), hint: t("only PYTORCH_*, HIP_*, CUDA_*, … keys the launcher allows; values must not be paths") },
      { name: "note", label: t("Note"), value: base.note ?? "" },
    ], p && !copy ? t("Save") : t("Create"), { wide: true });
    if (!v) return;
    const env = Object.fromEntries(v.env.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => [l.split("=")[0], l.slice(l.indexOf("=") + 1)]));
    const body = { name: v.name.trim(), core: v.core.trim(), venv: v.venv.trim(), nodes: words(v.nodes), frontend: v.frontend.trim() || null,
                   flags: v.flags.split(/\s+/).filter(Boolean), env, note: v.note || null };
    if (p && !copy) act(() => api("PATCH", `/api/admin/profiles/${p.id}`, body), t("Profile saved"), refresh);
    else act(() => api("POST", "/api/admin/profiles", body), t("Profile created"), refresh);
  }
  // A recipe (publish.py recipe) leaves a profile template in the artifact store; a worker host's check reads it.
  async function fromRecipe() {
    const via = insts.find((i) => i.instance && ["ssh", "http-agent"].includes(i.instance.restart_backend));
    if (!via) return toast(t("no worker to read the artifact store through yet — set one up first (System → Workers)"), true);
    const r = await act(() => api("POST", `/api/admin/instances/${via.worker_id}/check`));
    if (!r) return;
    const line = (r.check || []).find((x) => x.name === "recipes");
    const templates = line?.templates || [];
    const buildable = (r.check || []).find((x) => x.name === "builds")?.recipes || [];
    if (!templates.length && !buildable.length) {
      return toast(line ? line.detail : t("this worker's tools are older than recipes — Set up again (System → Workers)"), true);
    }
    if (!templates.length) {                               // nothing published yet: build first, then come back here
      const b = await formDialog(t("Build a recipe on {name}", { name: via.worker }), [
        { name: "recipe", label: t("Recipe"), type: "select", value: buildable[0], options: buildable.map((x) => ({ value: x, label: x })),
          hint: t("the host downloads ComfyUI, PyTorch and the nodes and publishes them to its artifact store — minutes, mostly the PyTorch download") },
      ], t("Build"));
      if (b && await runBuild(via, b.recipe)) return fromRecipe();
      return;
    }
    let allow = "";
    try {
      const last = (await api("GET", "/api/admin/setups")).find((s) => s.spec?.router_url);
      if (last) allow = new URL(last.spec.router_url).hostname;
    } catch { /* a guess only */ }
    const v = await formDialog(t("Profiles from a recipe"), [
      { name: "template", label: t("Template"), type: "select", value: templates[templates.length - 1].template,
        options: templates.map((x) => ({ value: x.template, label: x.about ? `${x.template} — ${x.about}` : x.template })) },
      { name: "allow", label: "COMFYFARM_ALLOW", value: allow,
        hint: t("the addresses the workers take requests from: the router's and the gateway's, as the workers see them") },
    ], t("Create both profiles"), { wide: true });
    if (!v) return;
    const tpl = templates.find((x) => x.template === v.template);
    const made = [];
    for (const [role, what] of [["gpu", t("GPU workers")], ["ui", t("UI worker")]]) {
      const p = tpl[role];
      const res = await act(() => api("POST", "/api/admin/profiles", {
        name: `${tpl.template}-${role}`, core: p.core, venv: p.venv, nodes: p.nodes, frontend: null, flags: p.flags || [],
        env: { ...(p.env || {}), ...(v.allow.trim() ? { COMFYFARM_ALLOW: v.allow.trim() } : {}) },
        note: `${what} · ${tpl.template}` }));
      if (!res) return refresh();
      made.push(`${tpl.template}-${role}`);
    }
    toast(t("Profiles created: {names} — pick them in Set up a worker, or Assign", { names: made.join(", ") }));
    refresh();
  }
  // A build on a host that allows it (its setup): request, then follow its state until done or failed.
  async function runBuild(via, recipe) {
    if (!await act(() => api("POST", `/api/admin/instances/${via.worker_id}/build`, { recipe }))) return false;
    const state = h("div", { class: "section-head" }), log = h("pre", { class: "code log" });
    const dlg = h("dialog", { class: "form wide" });
    let timer = null, result = null, done;
    const finished = new Promise((r) => (done = r));
    const close = () => { clearTimeout(timer); dlg.close(); dlg.remove(); done(result); };
    dlg.append(h("h3", {}, t("Building {recipe} on {name}", { recipe, name: via.worker })), state, log,
      h("div", { class: "dialog-actions" }, button(t("Close"), close)));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
    const poll = async () => {
      try {
        const s = await api("GET", `/api/admin/instances/${via.worker_id}/build?recipe=${encodeURIComponent(recipe)}`);
        state.replaceChildren(pill(t(s.state === "none" ? "waiting" : s.state), s.state === "done" ? "ok" : s.state === "failed" ? "bad" : "warn"),
          " ", s.template ? t("template {name}", { name: s.template }) : s.error || "");
        log.textContent = s.log || "";
        log.scrollTop = log.scrollHeight;
        if (s.state === "done") { result = true; toast(t("Built — now pick the template")); return close(); }
        if (s.state === "failed") { result = false; return; }
      } catch (e) { state.textContent = e.message; }
      timer = setTimeout(poll, 3000);
    };
    poll();
    return finished;
  }
  const profCols = ["name", "core", "venv", "nodes", "flags", "assigned", "note", "actions"];
  function buildProf() {
    const tr = namedRow(profCols, { assigned: "num", actions: "actions" });
    tr.edit = button(t("Edit"), () => editProfile(tr._item));
    tr.c.actions.append(button(t("Copy"), () => editProfile(tr._item, true)), tr.edit,
      button(t("Delete"), () => { if (confirm(t("Delete profile {name}?", tr._item))) act(() => api("DELETE", `/api/admin/profiles/${tr._item.id}`), t("Deleted"), refresh); },
             { cls: "danger" }));
    return tr;
  }
  function updateProf(tr, p) {
    setText(tr.c.name, p.name); setText(tr.c.core, p.core); setText(tr.c.venv, p.venv);
    setText(tr.c.nodes, p.nodes.join(", ") || "—"); setText(tr.c.flags, p.flags.join(" ") || "—");
    setText(tr.c.assigned, p.assigned); setText(tr.c.note, p.note ?? "");
    tr.edit.title = p.assigned ? t("in use: only the note can change") : "";
  }

  // ---- rollouts ----------------------------------------------------------------------------------------------
  function stepsTable(ro) {
    const tb = table([["#", "num"], [t(t("Worker"))], [t("From → to")], [t("Step")], [t(t("Detail"))], [t("Error")], [t("Started")], [t("Took"), "num"]]);
    for (const s of ro.steps) {
      const took = s.started_at ? ((s.finished_at ? Date.parse(s.finished_at) : Date.now()) - Date.parse(s.started_at)) / 1000 : null;
      tb.tbody.append(h("tr", {}, h("td", { class: "num" }, String(s.position + 1)), h("td", {}, s.worker),
        h("td", {}, `${s.from ?? "—"} → ${s.to}`), h("td", {}, pill(s.status)), h("td", {}, s.detail ?? ""),
        h("td", { class: "err" }, s.error ?? ""), h("td", {}, s.started_at ? when(s.started_at) : ""),
        h("td", { class: "num" }, took == null ? "" : `${Math.round(took)}s`)));
    }
    return tb.el;
  }
  function renderActive(list) {
    const cur = list.find((r) => ACTIVE.includes(r.status));
    banner.replaceChildren(...(cur ? cur.steps.filter((s) => s.backend === "manual" && s.status === "restarting").map((s) =>
      h("div", { class: "callout" }, h("strong", {}, t("Your turn: ")),
        t("restart {instance} on its host now (e.g. systemctl restart comfyfarm@{instance}). The farm watches it go down and come back.", s))) : []));
    if (!cur) { active.replaceChildren(h("p", { class: "muted" }, t("No rollout running."))); return; }
    const buttons = [];
    if (cur.status === "running") buttons.push(button(t("Abort"), () => {
      if (confirm(t("Abort? Workers still draining go back to work; restarts already under way finish."))) act(() => api("POST", `/api/admin/rollouts/${cur.id}/abort`), t("Aborting"), refresh);
    }, { cls: "danger" }));
    if (cur.status === "interrupted") buttons.push(
      button(t("Resume"), () => act(() => api("POST", `/api/admin/rollouts/${cur.id}/resume`), t("Resumed"), refresh), { cls: "primary" }),
      button(t("Abort"), () => act(() => api("POST", `/api/admin/rollouts/${cur.id}/abort`), t("Aborted"), refresh), { cls: "danger" }));
    active.replaceChildren(
      h("div", { class: "section-head" }, h("h3", {}, `#${cur.id} → ${cur.profile}`), pill(cur.status),
        h("span", { class: "muted" }, t("by {who}, {when} · window {window}", { who: cur.created_by, when: when(cur.created_at), window: cur.window })), ...buttons),
      cur.status === "interrupted" ? h("div", { class: "callout" }, h("strong", {}, t("Interrupted: ")),
        t("the router restarted during this rollout. Its workers stay in maintenance until you resume or abort.")) : "",
      stepsTable(cur));
  }
  function renderHistory(list) {
    const done = list.filter((r) => !ACTIVE.includes(r.status)).slice(0, 8);
    history.replaceChildren(...(done.length ? [h("h3", {}, t("Recent rollouts")), ...done.map((r) => h("details", {},
      h("summary", {}, pill(r.status), " " + t("#{id} → {profile} · {done}/{steps} done · {when} · {who}", { id: r.id, profile: r.profile,
        done: r.steps.filter((s) => s.status === "done").length, steps: r.steps.length, when: when(r.finished_at || r.created_at), who: r.created_by })),
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
      auditT.tbody.replaceChildren(...a.map((e) => h("tr", {}, h("td", { class: "nowrap" }, when(e.at)), h("td", { class: "nowrap" }, e.actor),
        h("td", { class: "nowrap" }, e.action), h("td", { class: "nowrap" }, e.target ?? ""), h("td", { class: "muted small" }, Object.keys(e.detail || {}).length ? JSON.stringify(e.detail).slice(0, 160) : ""))));
    },
  };
}
