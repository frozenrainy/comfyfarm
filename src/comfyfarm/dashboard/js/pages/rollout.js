// Rollout tab (docs/design/07): how each worker is launched (instance), what it should run (profile), and rolling a
// profile out one worker at a time — drain, restart, wait for the badge, verify, revert that worker on failure.
import { act, api, button, formDialog, h, namedRow, pill, qs, setPill, setText, syncRows, t, table, toast, when } from "../core.js";
import { checkDetail, checkHost } from "./setup.js";

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
  let profiles = [], insts = [], artifacts = null, builder = null, packs = [];
  const banner = h("div");
  const firstStart = h("div"), buildInfo = h("div");
  let builds = [];
  const packInfo = h("p", { class: "muted small" });
  const active = h("div");
  const instT = table([[t("Worker")], [t("Instance")], [t("Profile")], [t("Badge")], [t("Rev")], [t("Running")], [t("Restart via")], ["", "actions"]]);
  const profT = table([[t("Profile")], [t("Core")], [t("Venv")], [t("Nodes")], [t("Flags")], [t(t("Assigned")), "num"], [t("Note")], ["", "actions"]]);
  const packT = table([[t("Node pack")], [t("Repository")], [t("Commit")], [t("UI worker")], [t("Note")], ["", "actions"]]);
  const auditT = table([[t("When")], [t("Who")], [t("What")], [t("Target")], [t("Detail")]]);
  // the audit log a page at a time; an older page stays put while new entries come in (upto: the newest when it left)
  const AUDIT_PAGE = 20;
  const audit = { offset: 0, upto: null, top: null };
  const auditInfo = h("span", { class: "muted" });
  const auditPrev = button(t("← Newer"), () => { audit.offset = Math.max(0, audit.offset - AUDIT_PAGE); if (!audit.offset) audit.upto = null; refresh(); });
  const auditNext = button(t("Older →"), () => { audit.upto ??= audit.top; audit.offset += AUDIT_PAGE; refresh(); });
  const history = h("div");

  const startBtn = button(t("Start rollout"), async () => {
    const ready = insts.filter((i) => i.instance);
    if (!profiles.length || !ready.length) return act(async () => { throw new Error(t("needs a profile and at least one configured instance")); });
    // a UI profile (--cpu) goes to the UI worker, a GPU profile to the GPU workers: only those are ticked
    const forUi = (pid) => (profiles.find((p) => String(p.id) === pid)?.flags || []).includes("--cpu");
    const fitting = (pid) => ready.filter((i) => !i.maintenance && (i.instance.role === "ui") === forUi(pid)).map((i) => String(i.worker_id));
    const gpuProfiles = profiles.filter((p) => !(p.flags || []).includes("--cpu"));
    const first = String((gpuProfiles.length ? gpuProfiles : profiles).reduce((a, b) => (b.id > a.id ? b : a)).id);   // the newest
    const v = await formDialog(t("Start a rollout"), [
      { name: "profile", label: t("Roll out profile"), type: "select", value: first,
        options: profiles.map((p) => ({ value: String(p.id), label: p.name + ((p.flags || []).includes("--cpu") ? ` (${t("UI worker")})` : "") })),
        onChange: (pid, set) => set("workers", fitting(pid)) },
      { name: "workers", label: t("Workers (one at a time unless the window is larger)"), type: "checkboxes",
        value: fitting(first),
        options: ready.map((i) => ({ value: String(i.worker_id), label: t("{worker} — now {profile} ({badge})", { worker: i.worker, profile: i.profile ?? t("unassigned"), badge: t(i.badge) })
          + (i.instance.role === "ui" ? ` · ${t("UI worker")}` : "") })) },
      { name: "window", label: t("Window (workers out of service at the same time)"), type: "number", value: 1, min: 1 },
      { name: "drain", label: t("Give up draining after (minutes, 0 = wait for the running job however long)"), type: "number", value: 0, min: 0 },
      { name: "start", label: t("Wait for the new version at most (minutes)"), type: "number", value: 15, min: 1 },
      { name: "smoke", label: t("Verify"), type: "checkbox", text: t("also run a tiny smoke workflow on each worker") },
    ], t("Start"));
    if (!v) return;
    if (!v.workers.length) return toast(t("pick at least one worker"), true);
    const options = { start_timeout: (v.start || 15) * 60, smoke: v.smoke };
    if (v.drain) options.drain_timeout = v.drain * 60;
    const res = await act(() => api("POST", "/api/admin/rollouts", { profile_id: Number(v.profile), worker_ids: v.workers.map(Number),
                                                                   window: v.window || 1, options }), t("Rollout started"), refresh);
    if (res?.warnings?.length) alert(t("Started, with warnings:") + "\n\n" + res.warnings.join("\n"));
  }, { cls: "primary" });

  root.append(
    h("section", {}, head(t("Rollout"), startBtn), firstStart, buildInfo, banner, active, history),
    h("section", {}, head(t("Instances")), h("p", { class: "muted small" },
      t("How the launcher on each host starts the worker. Badge: match = the worker runs exactly its assigned render; " +
      "differs = it runs something else (not restarted yet, or a failed start).")), instT.el),
    h("section", {}, head(t("Profiles"), button(t("From a recipe"), () => fromRecipe(), { cls: "primary" }),
                          button(t("New profile"), () => editProfile(null))),
      h("p", { class: "muted small" }, t("A profile in use cannot be changed — copy it and roll the copy out.")), profT.el),
    h("section", {}, head(t("Node packs"), button(t("Build"), () => buildOnPlane(), { cls: "primary" }),
                          button(t("Add node pack"), () => addPack())), packInfo, packT.el),
    h("section", {}, head(t("Audit log")), auditT.el, h("div", { class: "pager" }, auditPrev, auditInfo, auditNext)));

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
        options: Object.keys(BACKEND_HINT).map((b) => ({ value: b, label: ADVANCED.includes(b) ? t("{name} (advanced)", { name: b })
                                                                         : b === "manual" ? t("manual") : b })) },
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
  // A worker that is not running (added before any profile existed, or stopped) is started here; a running one changes
  // through a rollout, which drains it first.
  const startable = (i) => i.instance && i.state !== "online" && i.enabled && i.instance.restart_backend !== "manual";
  async function assign(i) {
    const down = startable(i);
    const v = await formDialog(t("Assign a profile to {worker}", i), [
      { name: "profile", label: t("Profile"), type: "select", value: String(i.instance.profile_id ?? profiles[0]?.id ?? ""),
        options: profiles.map((p) => ({ value: String(p.id), label: p.name })),
        hint: down ? t("The worker is not running: it starts with this profile.")
                   : t("Without a restart: the badge shows 'differs' until the worker restarts. To change a running worker, start a rollout.") },
      ...(down ? [{ name: "start", type: "checkbox", value: true, text: t("Start it now") }] : [])],
      down ? t("Assign and start") : t("Assign"));
    if (!v) return;
    const res = await act(() => api("POST", `/api/admin/instances/${i.worker_id}/assign`, { profile_id: Number(v.profile) }),
                          v.start ? null : t("Assigned"), v.start ? null : refresh);
    if (res?.warnings?.length) alert(res.warnings.join("\n"));
    if (res && v.start) await start(i);
  }
  // Restart a running worker the safe way: a rollout of the profile it runs, restarting even though nothing changed in
  // the profile — it finishes its job first, then comes back with what changed on its host (Set up again: tools, env).
  async function restartWorker(i) {
    if (!confirm(t("Restart {worker}? It finishes its running job first (a rollout of its own profile).", i))) return;
    await act(() => api("POST", "/api/admin/rollouts", { profile_id: i.instance.profile_id, worker_ids: [i.worker_id], window: 1,
                                                         options: { restart: true } }), t("Restarting {worker} — see Rollout", i), refresh);
  }
  async function start(i) {
    await act(() => api("POST", `/api/admin/instances/${i.worker_id}/start`),
              t("Starting {worker} — its badge turns to match when it is up", i), refresh);
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
    tr.go = button(t("Start"), () => start(tr._item), { cls: "primary",
                   title: t("The worker is not running: start it with its assigned profile") });
    tr.re = button(t("Restart"), () => restartWorker(tr._item),
                   { title: t("Drain, restart and verify it on its own profile — after Set up again, to use the new tools") });
    tr.ren = button(t("Render"), () => showRender(tr._item));
    tr.chk = button(t("Check host"), () => checkHost(tr._item.worker_id, tr._item.worker),
                    { title: t("Read-only check of the host through its agent: driver, groups, directories, venvs, clock, router") });
    tr.c.actions.append(tr.go, tr.re, tr.cfg, tr.asg, tr.ren, tr.chk);
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
    tr.go.hidden = !(startable(i) && i.instance.profile_id);
    tr.re.hidden = !(i.instance && i.instance.profile_id && i.state === "online" && i.instance.restart_backend !== "manual");
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
  // A recipe (publish.py recipe) leaves a profile template in the artifact store. With the control-plane builder the
  // router reads the store itself; otherwise a worker host's check reads it (through a GPU worker when there is one).
  async function storeTemplates() {
    if (builder?.artifacts) {
      return { all: await api("GET", "/api/admin/templates"), buildable: builder.up ? builder.recipes : [], via: null };
    }
    const rank = (i) => (i.instance.role !== "ui" ? 0 : 4) + (i.enabled ? 0 : 2) + (i.state === "online" ? 0 : 1);
    const via = insts.filter((i) => i.instance && ["ssh", "http-agent"].includes(i.instance.restart_backend))
                     .sort((x, y) => rank(x) - rank(y))[0];
    if (!via) throw new Error(t("no worker to read the artifact store through yet — set one up first (System → Workers)"));
    const check = (await api("POST", `/api/admin/instances/${via.worker_id}/check`)).check || [];
    const line = check.find((x) => x.name === "recipes"), store = check.find((x) => x.name === "artifact store");
    if (!line) {
      if (store?.level === "FAIL") throw new Error(t("the artifact store on {name} is not usable: {detail}", { name: via.worker, detail: checkDetail(store) }));
      throw new Error(t("this worker's tools are older than recipes — Set up again (System → Workers)"));
    }
    return { all: line.templates || [], buildable: check.find((x) => x.name === "builds")?.recipes || [], via, detail: checkDetail(line) };
  }
  async function fromRecipe(pick = null) {
    let got;
    try { got = await storeTemplates(); } catch (e) { return toast(e.message, true); }
    const { all, buildable, via } = got;
    const age = (a, b) => String(a.created || "").localeCompare(String(b.created || ""));     // the store lists by name
    const templates = all.filter((x) => !(x.missing || []).length).sort(age), stale = all.filter((x) => (x.missing || []).length);
    const where = via ? via.worker : t("the control plane");
    if (!templates.length && !buildable.length) {
      return toast(stale.length ? t("no template is usable: {names}", { names: stale.map((x) => `${x.template} ${t("(artifacts missing)")}`).join(", ") })
                                : got.detail || t("no template in the artifact store yet"), true);
    }
    const buildFirst = async (recipe = null) => {
      if (!via) return buildOnPlane(recipe);
      const b = await formDialog(t("Build a recipe on {name}", { name: where }), [
        { name: "recipe", label: t("Recipe"), type: "select", value: buildable.includes(recipe) ? recipe : buildable[0],
          options: buildable.map((x) => ({ value: x, label: x })),
          hint: t("the host downloads ComfyUI, PyTorch and the nodes and publishes them to its artifact store — minutes, mostly the PyTorch download") },
      ], t("Build"));
      const built = b && await runBuild(via, b.recipe);
      if (built) return fromRecipe(typeof built === "string" ? built : null);
    };
    if (!templates.length) return buildFirst();            // nothing published yet: build, then come back here
    let allow = "";
    try {
      const last = (await api("GET", "/api/admin/setups")).find((x) => x.spec?.router_url);
      const host = last ? new URL(last.spec.router_url).hostname : "";
      if (/^[0-9.]+$/.test(host)) allow = host;                 // the allow-list takes addresses, not names
    } catch { /* a guess only */ }
    const v = await formDialog(via ? t("Profiles from a recipe (read through {name})", { name: via.worker }) : t("Profiles from a recipe"), [
      { name: "template", label: t("Template"), type: "select",
        hint: (() => { const r = builds.find((x) => x.state === "running"); return r ? t("A build of {recipe} is running: its template appears here when it is done", r) : ""; })(),
        value: templates.some((x) => x.template === pick) ? pick : templates[templates.length - 1].template,
        tip: t("What one build left in the artifact store: ComfyUI, a venv and the node sets for the GPU workers and the UI worker. The newest is last."),
        options: templates.map((x) => ({ value: x.template, label: (x.about ? `${x.template} — ${x.about}` : x.template)
          + ((x.roles || ["gpu", "ui"]).length < 2 ? ` (${(x.roles || []).map((r) => r === "ui" ? t("UI worker") : t("GPU workers")).join("")})` : "") })) },
      { name: "allow", label: "COMFYFARM_ALLOW", value: allow,
        tip: t("Optional: addresses the workers take requests from, added to what setup allowed on each host (the router's address). Leave it empty unless a worker reaches the router or the gateway over another address."),
        hint: t("optional — usually empty: setup already lets the router and the gateway in on each host") },
      ...(buildable.length ? [{ name: "build", type: "checkbox", value: false, label: t("Build"),
                               text: t("Build the chosen template's recipe again on {name} first", { name: where }),
                               tip: t("Makes a new template instead of using one of these: after adding or updating node packs, or after a ComfyFarm upgrade that changed the recipe. The build takes minutes; its profiles then roll out like any.") }] : []),
    ], t("Create the profiles"), { wide: true });
    if (!v) return;
    const tpl = templates.find((x) => x.template === v.template);
    if (v.build) return buildFirst(tpl?.recipe);
    const made = [], kept = [];
    for (const [role, what] of [["gpu", t("GPU workers")], ["ui", t("UI worker")]]) {
      if (!tpl[role]) continue;                                // a template may hold one half (the first start's: UI only)
      const name = `${tpl.template}-${role}`, p = tpl[role];
      try {
        await api("POST", "/api/admin/profiles", {
          name, core: p.core, venv: p.venv, nodes: p.nodes, frontend: null, flags: p.flags || [],
          env: { ...(p.env || {}), ...(v.allow.trim() ? { COMFYFARM_ALLOW: v.allow.trim() } : {}) }, note: `${what} · ${tpl.template}` });
        made.push(name);
      } catch (e) {
        if (e.status === 409) { kept.push(name); continue; }      // a second click, or half of a pair
        toast(e.message, true);
        return refresh();
      }
    }
    if (made.length) {
      toast((packs.length ? t("Profiles created: {names} — roll the UI profile out to the UI worker (Start rollout) for the node packs; the GPU profile goes to GPU workers (Set up a worker, or a rollout)", { names: made.join(", ") })
                          : t("Profiles created: {names} — next, Set up a worker picks the GPU profile (the UI worker already runs what it needs)", { names: made.join(", ") }))
            + (kept.length ? " " + t("({names} existed already — kept)", { names: kept.join(", ") }) : ""));
    } else if (kept.length) {
      toast(t("{names}: these profiles exist already — this template made them. Roll them out as they are; for new node packs or a new recipe, build again (the checkbox below the template).", { names: kept.join(", ") }));
    }
    refresh();
  }
  // A build, followed until done or failed. On a worker host (its setup allowed builds) the host answers "queued" until
  // its build service takes the request, so the last run's state is never read as this one's; on the control plane
  // each request has its own id.
  async function runBuild(via, recipe) {
    if (!await act(() => api("POST", `/api/admin/instances/${via.worker_id}/build`, { recipe }))) return false;
    return followBuild(t("Building {recipe} on {name}", { recipe, name: via.worker }),
      () => api("GET", `/api/admin/instances/${via.worker_id}/build?recipe=${encodeURIComponent(recipe)}`));
  }
  async function followBuild(title, read) {
    const LOOK = { queued: "pending", none: "pending", running: "running", done: "done", failed: "failed" };
    const state = h("div", { class: "section-head" }), log = h("pre", { class: "code log" });
    const dlg = h("dialog", { class: "form wide" });
    let timer = null, result = null, closed = false, done;
    const finished = new Promise((r) => (done = r));
    const close = () => { closed = true; clearTimeout(timer); dlg.close(); dlg.remove(); done(result); };
    dlg.append(h("h3", {}, title), state, log, h("div", { class: "dialog-actions" }, button(t("Close"), close)));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
    const poll = async () => {
      if (closed) return;
      try {
        const s = await read();
        if (closed) return;
        state.replaceChildren(pill(LOOK[s.state] || "pending", t(s.state === "none" ? "waiting" : s.state)), " ",
          s.template ? t("template {name}", { name: s.template }) : s.error || "");
        log.textContent = s.log || "";
        log.scrollTop = log.scrollHeight;
        if (s.state === "done") { result = s.template || true; toast(t("Built — now pick the template")); return close(); }
        if (s.state === "failed") { result = false; return; }
      } catch (e) { if (closed) return; state.textContent = e.message; }
      timer = setTimeout(poll, 3000);
    };
    poll();
    return finished;
  }
  // ---- node packs and builds on the control plane (docs/design/15) -------------------------------------------
  async function waitFor(rid, seconds = 90) {
    for (let i = 0; i < seconds; i++) {
      const s = await api("GET", `/api/admin/builds/${rid}`);
      if (s.state === "done" || s.state === "failed") return s;
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error(t("the builder did not answer in time"));
  }
  async function resolve(repo, ref) {
    const r = await api("POST", "/api/admin/node-packs/resolve", { repo, ref });
    const s = await waitFor(r.id);
    if (s.state !== "done") throw new Error(s.error || t("could not look it up"));
    return s.commit;
  }
  const PACK_TIP = t("Off for a pack that imports only with a GPU (the UI worker runs on the CPU) — a rollout to the UI worker names such a pack");
  async function addPack() {
    if (!builder?.up) return toast(t("the builder is not running — node packs are looked up and built by it"), true);
    if (!builder.node_sources.length) return toast(t("this farm's builder takes no node packs from the dashboard (COMFYFARM_BUILD_NODE_SOURCES)"), true);
    const v = await formDialog(t("Add a node pack"), [
      { name: "repo", label: t("Git repository (https)"), placeholder: "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
        hint: t("allowed here: {sources}", { sources: builder.node_sources.join(", ") }) },
      { name: "ref", label: t("Branch, tag or commit (empty: the default branch)"), placeholder: "main",
        hint: t("pinned to the commit it names now; Update moves it to a newer one") },
      { name: "name", label: t("Name (its directory under custom_nodes; empty: from the repository)") },
      { name: "ui", type: "checkbox", value: true, label: t("UI worker"), text: t("Also on the UI worker"), hint: PACK_TIP },
      { name: "requirements_file", label: t("Requirements file (empty: requirements.txt, else pyproject.toml)"), placeholder: "requirements.txt" },
      { name: "constraints", label: t("Constraints (optional, comma-separated)"), placeholder: "kornia<0.8",
        hint: t("hold a dependency back where the pack breaks on a newer one") },
      { name: "note", label: t("Note") },
    ], t("Look up and add"), { wide: true });
    if (!v) return;
    const repo = v.repo.trim().replace(/\/+$/, "");
    const name = v.name.trim() || repo.split("/").pop().replace(/\.git$/, "");
    await act(async () => {
      toast(t("Looking up {ref}…", { ref: v.ref.trim() || t("the default branch") }));
      const commit = await resolve(repo, v.ref.trim());
      return api("POST", "/api/admin/node-packs", { name, repo, commit, ref: v.ref.trim() || null, ui: !!v.ui,
        requirements_file: v.requirements_file.trim() || null, constraints: words(v.constraints), note: v.note.trim() || null });
    }, t("Node pack added — Build to put it into the artifact store"), refresh);
  }
  async function updatePack(p) {
    await act(async () => {
      const commit = await resolve(p.repo, p.ref || "");
      if (commit === p.commit) return "same";
      return api("PATCH", `/api/admin/node-packs/${p.id}`, { commit });
    }, (r) => r === "same" ? t("Already the newest commit of {ref}", { ref: p.ref || t("the default branch") }) : t("Updated — Build to use it"), refresh);
  }
  async function editPack(p) {
    const v = await formDialog(t("Node pack {name}", p), [
      { name: "ui", type: "checkbox", value: p.ui, label: t("UI worker"), text: t("Also on the UI worker"), hint: PACK_TIP },
      { name: "requirements_file", label: t("Requirements file (empty: requirements.txt, else pyproject.toml)"), value: p.requirements_file ?? "" },
      { name: "constraints", label: t("Constraints (optional, comma-separated)"), value: (p.constraints || []).join(", ") },
      { name: "note", label: t("Note"), value: p.note ?? "" },
    ], t("Save"));
    if (!v) return;
    act(() => api("PATCH", `/api/admin/node-packs/${p.id}`, { ui: !!v.ui, requirements_file: v.requirements_file.trim(),
      constraints: words(v.constraints), note: v.note }), t("Saved — Build to use it"), refresh);
  }
  async function buildOnPlane(recipe = null) {
    if (!builder?.configured) return toast(t("no control-plane builder here — build on a worker host (From a recipe) or with publish.py"), true);
    if (!builder.up) return toast(t("the builder is not running (docker compose ps builder)"), true);
    // the recipe for the GPU the farm's workers have, when they say (their ComfyUI reports cuda or rocm)
    const backends = new Set(insts.filter((i) => i.instance && i.instance.role !== "ui").map((i) => i.backend).filter(Boolean));
    const guess = builder.recipes.includes(recipe) ? recipe
                : builder.recipes.find((r) => [...backends].some((b) => r.startsWith(b))) || "";
    const v = await formDialog(t("Build on the control plane"), [
      { name: "recipe", label: t("Recipe (the kind of GPU)"), type: "select", value: guess, required: true,
        options: [...(guess ? [] : [{ value: "", label: t("— pick the kind of GPU your workers have —") }]),
                  ...builder.recipes.map((x) => ({ value: x, label: builder.about?.[x] ? `${x} — ${builder.about[x]}` : x }))],
        tip: t("Which PyTorch the GPU workers get: rocm-… for AMD, cuda-… for NVIDIA (see the description). The UI worker's part is the same in every recipe.") },
      { name: "roles", label: t("For"), type: "select", value: "both", options: [
        { value: "both", label: t("GPU workers and the UI worker") }, { value: "gpu", label: t("GPU workers only") },
        { value: "ui", label: t("the UI worker only") }] },
      // the packs by name, here in the dialog ("below" read as below the checkbox, where nothing was)
      { name: "packs", type: "checkbox", value: packs.length > 0, label: t("Node packs"),
        text: t("with this farm's node packs ({n})", { n: packs.length }),
        hint: packs.length ? packs.map((p) => p.name + (p.ui ? "" : ` (${t("GPU only")})`)).join(", ")
                           : t("none added yet — Add node pack first (System → Rollout → Node packs)") },
    ], t("Build"));
    if (!v) return;
    const r = await act(() => api("POST", "/api/admin/builds", { recipe: v.recipe, roles: v.roles === "both" ? ["gpu", "ui"] : [v.roles], packs: !!v.packs }));
    if (!r) return;
    const tpl = await followBuild(t("Building {recipe} on the control plane", { recipe: v.recipe }), () => api("GET", `/api/admin/builds/${r.id}`));
    if (tpl) fromRecipe(typeof tpl === "string" ? tpl : null);
  }
  function buildPack() {
    const tr = namedRow(["name", "repo", "commit", "ui", "note", "actions"], { actions: "actions" });
    tr.c.actions.append(button(t("Update"), () => updatePack(tr._item), { title: t("look up the newest commit of its branch or tag") }),
      button(t("Edit"), () => editPack(tr._item)),
      button(t("Remove"), () => { if (confirm(t("Remove node pack {name}? Profiles built with it keep it until you build again.", tr._item))) act(() => api("DELETE", `/api/admin/node-packs/${tr._item.id}`), t("Removed"), refresh); },
             { cls: "danger" }));
    return tr;
  }
  function updatePackRow(tr, p) {
    setText(tr.c.name, p.name); setText(tr.c.repo, p.repo.replace(/^https:\/\//, ""));
    setText(tr.c.commit, p.commit.slice(0, 12) + (p.ref ? ` (${p.ref})` : ""));
    setText(tr.c.ui, p.ui ? t("yes") : t("no — GPU only")); setText(tr.c.note, p.note ?? "");
  }
  function renderBuilds(list) {
    const b = list.find((x) => x.state === "running") || list[0];
    const recent = b && (b.state === "running" || (b.finished && Date.now() - Date.parse(b.finished) < 3600e3));
    if (!recent) return buildInfo.replaceChildren();
    const what = { recipe: b.recipe, min: Math.max(1, Math.round((Date.now() - Date.parse(b.started)) / 60000)) };
    const follow = () => followBuild(t("Building {recipe} on the control plane", what), () => api("GET", `/api/admin/builds/${b.id}`))
      .then((tpl) => { if (tpl) fromRecipe(typeof tpl === "string" ? tpl : null); });
    buildInfo.replaceChildren(h("div", { class: "callout" }, ...(b.state === "running"
      ? [pill("running", t("building")), " ", t("Build of {recipe} on the control plane — {min} min so far", what), " ", button(t("Show"), follow)]
      : b.state === "done"
      ? [pill("done", t("done")), " ", t("Build of {recipe} done: template {template}", { ...what, template: b.template || "?" }), " ",
         button(t("From a recipe"), () => fromRecipe(b.template || null), { cls: "primary" })]
      : [pill("failed", t("failed")), " ", t("Build of {recipe} failed: {error}", { ...what, error: b.error || "" }), " ", button(t("Show the log"), follow)])));
  }
  function renderBuilder(b) {
    const fs = b.first_start || {};
    const LOOK = { building: "running", starting: "running", waiting: "pending", error: "failed" };
    firstStart.replaceChildren(...(LOOK[fs.state] ? [h("div", { class: "callout" }, pill(LOOK[fs.state], t(fs.state)), " ",
      h("strong", {}, t("UI worker: ")), t(fs.detail || ""))] : []));
    packInfo.textContent = !b.configured ? t("Without the control-plane builder (the compose deployment's), node packs go into recipes (rollouts.md).")
      : !b.up ? t("The builder is not running — node packs cannot be looked up or built.")
      : b.node_sources.length ? t("Added packs are built into node sets and venvs by Build, then rolled out like any profile — the UI worker first. Allowed sources: {s}", { s: b.node_sources.join(", ") })
      : t("This farm's builder takes no node packs from the dashboard (COMFYFARM_BUILD_NODE_SOURCES is empty).");
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
      const [i, p, r, a, b, np] = await Promise.all([api("GET", "/api/admin/instances"), api("GET", "/api/admin/profiles"),
        api("GET", "/api/admin/rollouts?limit=10"), api("GET", `/api/admin/audit/page?${qs({ offset: audit.offset, limit: AUDIT_PAGE, upto: audit.upto ?? "" })}`), api("GET", "/api/admin/builder"),
        api("GET", "/api/admin/node-packs")]);
      insts = i; profiles = p; builder = b; packs = np;
      renderBuilder(b);
      builds = b.configured ? await api("GET", "/api/admin/builds").catch(() => []) : [];
      renderBuilds(builds);
      syncRows(packT.tbody, np, (x) => x.id, buildPack, updatePackRow);
      if (artifacts === null) artifacts = await api("GET", "/api/admin/artifacts");
      syncRows(instT.tbody, i, (x) => x.worker_id, buildInst, updateInst);
      syncRows(profT.tbody, p, (x) => x.id, buildProf, updateProf);
      renderActive(r);
      const sig = JSON.stringify(r.filter((x) => !ACTIVE.includes(x.status)).map((x) => [x.id, x.status]));
      if (history.dataset.v !== sig) { history.dataset.v = sig; renderHistory(r); }
      // the actions are fixed English words from the router: shown in the page's language (names stay as they are)
      audit.top = a.upto;
      setText(auditInfo, a.total ? t("{from}–{to} of {total}", { from: a.offset + 1, to: a.offset + a.items.length, total: a.total }) : "");
      auditPrev.disabled = audit.offset === 0;
      auditNext.disabled = a.offset + a.items.length >= a.total;
      auditT.tbody.replaceChildren(...a.items.map((e) => h("tr", {}, h("td", { class: "nowrap" }, when(e.at)),
        h("td", { class: "nowrap" }, ["comfyfarm (first start)", "worker setup"].includes(e.actor) ? t(e.actor) : e.actor),
        h("td", { class: "nowrap" }, t(e.action)), h("td", { class: "nowrap" }, e.target ?? ""), h("td", { class: "muted small" }, Object.keys(e.detail || {}).length ? JSON.stringify(e.detail).slice(0, 160) : ""))));
    },
  };
}
