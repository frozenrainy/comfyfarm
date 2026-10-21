// Worker setup (docs/design/14): describe the host → run the setup script on it (the operator, or the router over ssh —
// the operator chooses, D-41) → read the host check → add the worker, configure its instance, assign, start.
import { act, api, button, formDialog, h, pill, setText, t, toast } from "../core.js";

const GPUS = [["amd", "AMD (ROCm)"], ["nvidia", "NVIDIA (CUDA)"], ["any", "Any / not sure"], ["none", "None (CPU only)"]];
const PATHS = [["art", "Artifact store <ART> (same path on every host)", "/srv/comfyfarm/art"],
               ["local", "Local root <LOCAL> (this host's disk)", "/var/lib/comfyfarm/local"],
               ["models", "Models directory", "/srv/comfyfarm/models"],
               ["input", "Input directory (shared)", "/mnt/farm/input"],
               ["output", "Output directory (shared)", "/mnt/farm/output"],
               ["user", "User directory (shared)", "/mnt/farm/user"],
               ["temp", "Temp directory (this host)", "/var/lib/comfyfarm/temp/{instance}"]];   // [key, English label, default]

function checkTable(rows) {
  return h("table", {}, h("tbody", {}, ...rows.map((r) => h("tr", {}, h("td", {}, pill(r.level, t(r.level))),
    h("td", { class: "nowrap" }, r.name), h("td", { class: "muted small" }, r.detail)))));
}

// Step 1: what the host should become. Defaults: the last setup's answers (a farm's hosts look alike).
// Hover text for every field of the setup form (what it is, what it must match).
const TIPS = {
  name: "How this worker appears in System → Workers and in the job history.",
  host: "The address (IP or host name) the router and the gateway reach this host at: the router logs in here over ssh, the gateway opens ComfyUI here.",
  instance: "Names this ComfyUI on the host — a host can run several, each with its own port and GPU. Used in the unit comfyfarm@<instance> and wherever a path says {instance}.",
  gpu: "The kind of GPU this host should have. The host check fails when none of that kind is usable. ComfyFarm checks the driver; it never installs one.",
  group: "Jobs run on the workers of a group, and users are allowed groups. Workers of one group must have the same custom nodes and models.",
  profile: "What this worker runs: a ComfyUI release, a venv and node sets. Leave it for later while nothing is published — System → Rollout → Profiles → From a recipe makes profiles.",
  port: "The port this ComfyUI listens on — one per instance on a host, and not the router's port when they share a host.",
  ssh_port: "The host's ssh server: the router's restart key logs in here.",
  account: "A dedicated account that runs ComfyUI; setup creates it (or uses one that exists). Never a person's login account: its authorized_keys gets the router's key.",
  uid: "The same number on every host, so that files on shared storage belong to one owner everywhere.",
  router_url: "The router as this host reaches it: http://<control-plane address>:<router port>. The launcher fetches what to run from here.",
  path_art: "Where published ComfyUI releases, venvs and node sets live — at the same path on every host (a shared mount or a copy).",
  path_local: "This host's own disk: its copies of ComfyUI and the node sets, and run files.",
  path_models: "The models ComfyUI reads (checkpoints, LoRAs, VAE …). Read only.",
  path_input: "Uploads. Shared by every worker and the UI worker: the same storage everywhere — never {instance}. Mount it before setup: setup never mounts storage.",
  path_output: "Results. Shared by every worker and the UI worker: the same storage everywhere — never {instance}. Mount it before setup: setup never mounts storage.",
  path_user: "Users' workflows and settings. Shared by every worker and the UI worker — never {instance}. Mount it before setup: setup never mounts storage.",
  path_temp: "Previews and other temporary files of this instance, on this host. {instance} becomes the instance name.",
  builds: "Lets System → Rollout → Profiles → From a recipe ask this host to build a recipe installed here. A separate build account owns the artifact store; ComfyUI's account only reads it. Needs internet and git; setup brings a pinned uv.",
};

// again: { worker_id, spec, extra } of the worker's last setup — the same host set up once more (new worker tools,
// builds on or off); its name and instance stay, and the last step updates it instead of adding a worker.
// fix: a setup that failed — the form comes back with its values; the new setup replaces it (its command stops working).
export async function setupWorker(refresh, again = null, fix = null) {
  if (fix && fix.extra.again && !again) again = { worker_id: fix.extra.again, spec: fix.spec, extra: fix.extra };
  // unfinished setups can be continued: the dialog may have been closed while the host ran the script
  const open = (await api("GET", "/api/admin/setups")).filter((x) => ["waiting", "failed", "reported"].includes(x.status));
  const src = fix || again;                               // where the form's values come from
  const pickRole = src ? { role: src.spec.role || "gpu" } : await formDialog(t("Set up a worker"), [
    { name: "role", label: t("What will this host be?"), type: "select", value: "gpu",
      options: [{ value: "gpu", label: t("GPU worker — runs the jobs") },
                { value: "ui", label: t("UI worker — serves the screen and the users' files (no GPU, no jobs)") },
                ...open.map((x) => ({ value: `continue:${x.id}`,
                  label: t("Continue: {name} on {host} ({status})", { name: x.spec.instance, host: x.extra.host, status: t(x.status) }) }))],
      hint: t("A farm has one UI worker (often on the control-plane host) and any number of GPU workers. The gateway finds the UI worker by itself.") },
  ], t("Next"));
  if (!pickRole) return;
  if (pickRole.role.startsWith("continue:")) {
    const s = open.find((x) => `continue:${x.id}` === pickRole.role);
    const made = s.status === "reported" ? { id: s.id, command: null }
      : await act(() => api("POST", `/api/admin/setups/${s.id}/renew`));
    if (made) runSetup({ ...made, instance: s.spec.instance, account: s.spec.account, again: s.extra.again }, refresh);
    return;
  }
  const ui = pickRole.role === "ui";
  const [groups, profiles, recent, builderState, dflt] = await Promise.all([api("GET", "/api/admin/groups"), api("GET", "/api/admin/profiles"),
    api("GET", "/api/admin/setups"), api("GET", "/api/admin/builder").catch(() => null), api("GET", "/api/admin/defaults").catch(() => ({}))]);
  // a first setup takes the deployment's own paths and router address (compose knows them); a later one, the last setup's
  const shared = dflt.shared ? { input: `${dflt.shared}/input`, output: `${dflt.shared}/output`, user: `${dflt.shared}/user` } : {};
  const routerGuess = (() => {
    const r = String(dflt.router || "");
    if (!r) return "";
    const m = /^(.*):(\d+)$/.exec(r);
    const host = m && m[1] && !["0.0.0.0", "::", "[::]"].includes(m[1]) ? m[1] : location.hostname;
    return `http://${host}:${m ? m[2] : r}`;
  })();
  const roleProfiles = profiles.filter((p) => ((p.flags || []).includes("--cpu")) === (pickRole?.role === "ui"));
  const last = src ? src.spec : recent[0]?.spec;
  const lastX = src ? src.extra : {};
  const pathOf = (k, d) => {
    if (k === "temp") {                                   // per instance: keep the template, not the last one's name
      const lt = last?.paths?.temp, li = last?.instance;
      return lt && li && lt.endsWith("/" + li) ? lt.slice(0, -li.length) + "{instance}" : d;
    }
    return (k === "art" ? last?.art : k === "local" ? last?.local : last?.paths?.[k]) ?? (k === "art" ? dflt.art : shared[k]) ?? d;
  };
  const keep = again ? t("kept: setting a worker up again changes its host settings, not its identity") : "";
  // The form comes back with what was typed when the router refuses it (a name taken, a bad path): nothing to retype.
  let typed = null, made = null, v = null, body = null, inst = null, errors = {};
  // a refusal that names a field ("host: …", "paths.input must be …", "instance name … is taken") marks that field
  const FIELD = [[/^host\b/, "host"], [/^instance\b/, "instance"], [/^router_url\b/, "router_url"], [/^account\b/, "account"],
                 [/^uid\b/, "uid"], [/^port\b/, "port"], [/^paths\.(\w+)/, (m) => `path_${m[1]}`],
                 [/^a worker with this name or address/, "name"]];
  const fieldOf = (msg) => { for (const [re, f] of FIELD) { const m = re.exec(msg); if (m) return typeof f === "function" ? f(m) : f; } return null; };
  while (!made) {
    const fields = [
      { name: "name", label: t("Worker name (shown in the dashboard)"), value: src ? lastX.name : ui ? "ui" : "", hint: keep },
      { name: "host", required: true, label: t("Host address"), value: lastX.host ?? "", placeholder: "10.0.0.21", hint: t("the address the router and the gateway reach this host at") },
      { name: "instance", required: true, label: t("Instance name"), value: src ? last.instance : ui ? "ui-1" : "gpu-1", hint: keep || t("one ComfyUI on this host; letters, digits, . _ -") },
      ...(ui ? [] : [
        { name: "gpu", label: t("GPU"), type: "select", value: last?.gpu && last.gpu !== "none" ? last.gpu : "amd",
          options: GPUS.map(([v, l]) => ({ value: v, label: t(l) })), hint: t("ComfyFarm checks the driver; it never installs one") },
        groups.length ? { name: "group", label: t("Group"), type: "select", value: src ? lastX.group ?? "" : groups[0]?.name ?? "",
          options: [{ value: "", label: t("(no group)") }, ...groups.map((g) => ({ value: g.name, label: g.name }))] }
        : { name: "group", label: t("Group"), value: "default",            // a new farm has none: made with the worker
            hint: t("no group yet: this one is made with the worker (jobs go to a group's workers; users get groups)") }]),
      { name: "profile", label: t("Profile to run"), type: "select",
        value: fix && lastX.profile_id ? String(lastX.profile_id) : again ? "" : String(roleProfiles[roleProfiles.length - 1]?.id ?? ""),
        options: [{ value: "", label: t("(assign later)") }, ...profiles.map((p) => ({ value: String(p.id), label: p.name }))],
        hint: ui ? t("UI profile: the core of the GPU workers, a CPU venv, nodes: comfyfarm_ui and comfyfarm_worker, flag --cpu, env COMFYFARM_ALLOW=<gateway and router addresses>") : "" },
      { name: "port", label: t("ComfyUI port"), type: "number", value: src ? last.port : ui ? 8189 : 8188,
        hint: ui ? t("not 8188 when it shares the host with the router") : "" },
      { name: "ssh_port", label: t("ssh port"), type: "number", value: lastX.ssh_port ?? 22 },
      { name: "account", label: t("Service account (its own, made by setup)"), value: last?.account ?? "comfyfarm",
        hint: t("not your login account: setup creates it to run ComfyUI, and only the router's key — restart, query, check — logs in as it") },
      { name: "uid", label: t("Service account uid"), type: "number", value: last?.uid ?? 47800,
        hint: t("the same number on every host: shared storage checks numbers, not names") },
      { name: "router_url", required: true, label: t("Router address as the worker sees it"), value: last?.router_url ?? routerGuess, placeholder: "http://10.0.0.5:8188",
        hint: t("http://<router address>:<router port> — the address the launcher fetches its render from") },
      ...PATHS.map(([k, label, d]) => ({ name: `path_${k}`, label: t(label), value: pathOf(k, d) })),
      // builds on a worker host: for farms without the control-plane builder (or a host that already has them on)
      ...(!builderState?.configured || (src && last?.builds) ? [
      { name: "builds", type: "checkbox", value: !!last?.builds, text: t("Allow builds from the dashboard on this host"),
        label: t("Builds"),
        hint: t("the dashboard may then ask this host to build a recipe that setup installs here (it names one, never sends one); a build account owns the artifact store, the service account still only reads it. Needs internet and git on the host; setup brings a pinned uv") }] : []),
    ];
    for (const f of fields) if (TIPS[f.name]) f.tip = t(TIPS[f.name]);
    if (typed) for (const f of fields) if (f.name in typed) f.value = typed[f.name];
    v = await formDialog(fix ? t("Correct the setup of {name}", { name: lastX.name || last.instance })
                         : again ? t("Set up {name} again", { name: lastX.name }) : ui ? t("Set up the UI worker") : t("Set up a GPU worker"),
                         fields, t("Next"), { wide: true, errors });
    if (!v) return;
    inst = v.instance.trim();
    body = {
      role: ui ? "ui" : "gpu", name: v.name.trim() || inst, host: v.host.trim(), instance: inst,
      gpu: ui ? "none" : v.gpu, group: ui ? null : v.group || null,
      profile_id: v.profile ? Number(v.profile) : null, port: v.port || 8188, ssh_port: v.ssh_port || 22,
      account: v.account.trim(), uid: v.uid || null, router_url: v.router_url.trim(),
      paths: Object.fromEntries(PATHS.map(([k]) => [k, v[`path_${k}`].trim().replace("{instance}", inst)])),
      builds: !!v.builds, worker_id: again ? again.worker_id : null, replaces: fix ? fix.id : null,
    };
    try {
      made = await api("POST", "/api/admin/setups", body);
    } catch (e) {
      toast(e.message, true);
      typed = v;
      const f = fieldOf(e.message);
      errors = f ? { [f]: e.message } : {};
    }
  }
  if (made) runSetup({ ...made, instance: inst, account: body.account, again: body.worker_id }, refresh);
}

// After setup without a profile (a new farm has none yet): pick one and start the worker now, or later in Rollout.
async function askStart(workerId, name, kind) {
  const profiles = (await api("GET", "/api/admin/profiles")).filter((p) => (kind === "ui") === (p.flags || []).includes("--cpu"));
  if (!profiles.length) return toast(t("Worker added — make a profile (System → Rollout → From a recipe), then assign and start it"));
  const v = await formDialog(t("Start {name} now?", { name }), [
    { name: "profile", label: t("Profile"), type: "select", value: String(profiles[profiles.length - 1].id),
      options: profiles.map((p) => ({ value: String(p.id), label: p.name })),
      hint: t("what it runs; a later change goes through a rollout") }], t("Assign and start"));
  if (!v) return toast(t("Worker added — assign a profile and start it in System → Rollout"));
  await act(async () => {
    await api("POST", `/api/admin/instances/${workerId}/assign`, { profile_id: Number(v.profile) });
    return api("POST", `/api/admin/instances/${workerId}/start`);
  }, t("Worker added and starting — watch its badge in System → Rollout"));
}

// Step 2: get root on the host — the operator picks the way; then watch the report come in.
function runSetup(made, refresh) {
  let mode = "command", timer = null, closed = false;
  const status = h("div", { class: "section-head setup-status" });
  const log = h("pre", { class: "code log", hidden: true });
  const checks = h("div");
  const finish = h("button", { type: "button", class: "primary", hidden: true, onclick: () => addWorker() },
                   made.again ? t("Update the worker") : t("Add the worker"));
  const cmd = h("pre", { class: "code wrap" }, made.command ?? t("(the host has reported — no need to run the script again)"));
  const copy = button(t("Copy"), () => navigator.clipboard?.writeText(made.command).then(() => toast(t("Copied")), () => {}));
  copy.hidden = !made.command;
  const user = h("input", { autocomplete: "off", placeholder: "admin" });
  const pw = h("input", { type: "password", autocomplete: "new-password" });
  const runSsh = button(t("Log in and run"), async () => {
    if (!user.value || !pw.value) return toast(t("account and password, please"), true);
    if (made.account && user.value.trim() === made.account) return toast(t("your own admin account here, not the service account"), true);
    const r = await act(() => api("POST", `/api/admin/setups/${made.id}/ssh`, { user: user.value.trim(), password: pw.value }));
    pw.value = "";                                           // not kept in the page either
    if (r) poll();
  }, { cls: "primary" });
  const paneCmd = h("div", { class: "stack" },
    h("p", {}, t("On the new host, as a user who may use sudo, run:")), cmd, h("div", {}, copy),
    h("p", { class: "muted small" }, t("It downloads the setup script from the router, checks it against this checksum and runs it with sudo. The router never holds root on the host. Valid for 2 hours.")));
  const paneSsh = h("div", { class: "stack", hidden: true },
    h("div", { class: "field" }, h("div", { class: "label" }, t("Your admin account on the host (may use sudo)")), user),
    h("div", { class: "field" }, h("div", { class: "label" }, t("Password")), pw),
    h("p", { class: "muted small" }, t("The router logs in with this account and runs the same script with sudo. The password is used for this run only: it is not stored, logged or shown again. While setup runs, the router has root on this host.")),
    h("div", { class: "pane-actions" }, runSsh));
  const choice = (key, title, text) => h("div", { class: `choice${key === mode ? " on" : ""}`, "data-k": key, onclick: () => pick(key) },
    h("h4", {}, title), h("p", {}, text));
  const choices = h("div", { class: "choices" },
    choice("command", t("Run a command yourself (recommended)"), t("You paste one line on the host. Nothing on the router ever gets root on your workers.")),
    choice("ssh", t("Let the router log in once"), t("Quicker for many hosts. You type an admin account and password; for those minutes the router has root on the host.")));
  function pick(k) {
    mode = k;
    for (const el of choices.children) el.classList.toggle("on", el.dataset.k === k);
    paneCmd.hidden = k !== "command"; paneSsh.hidden = k !== "ssh";
  }
  const dlg = h("dialog", { class: "form wide" });
  const close = () => { closed = true; clearTimeout(timer); dlg.close(); dlg.remove(); refresh?.(); };
  // not run yet or failed: correct the values (a new setup replaces this one) — or fix the host and run the same command again
  const fixBtn = button(t("Correct the values"), () => { const s = setup; close(); setupWorker(refresh, null, s); });
  fixBtn.hidden = true;
  dlg.append(h("h3", {}, t("Set up {name}", { name: made.instance || "" })), choices, paneCmd, paneSsh, status, log, checks,
    h("div", { class: "dialog-actions" }, button(t("Close"), close), fixBtn, finish));
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
  let setup = null;

  async function poll() {
    clearTimeout(timer);
    if (closed) return;
    try { setup = await api("GET", `/api/admin/setups/${made.id}`); } catch { timer = setTimeout(poll, 3000); return; }
    status.replaceChildren(pill(setup.status, t(setup.status)), " ",
      h("span", { class: "muted" }, setup.expired ? t("this setup expired — make a new one") : {
        waiting: t("waiting for the host to run the script…"), running: t("the router is running the script on the host…"),
        reported: t("the host is ready"), failed: t("setup or the host check failed — fix the host and run the same command again"),
        finished: t("the worker was added") }[setup.status] || ""));
    log.hidden = !setup.log; setText(log, setup.log);
    const r = setup.result;
    checks.replaceChildren(...(r ? [r.error ? h("div", { class: "callout" }, h("strong", {}, t("Error: ")), r.error) : "",
      r.check?.length ? checkTable(r.check) : ""] : []));
    finish.hidden = setup.status !== "reported";
    fixBtn.hidden = !["waiting", "failed"].includes(setup.status);
    if (["waiting", "running", "failed"].includes(setup.status) && !setup.expired) timer = setTimeout(poll, 2000);
  }

  // Step 3: the usual endpoints, in order — the same checks as doing it by hand.
  async function addWorker() {
    finish.disabled = true;
    try {
      if (made.again) {                                    // the same worker: instance, address, group and profile follow
        await api("PUT", `/api/admin/instances/${made.again}`, setup.instance_config);
        const w = setup.worker;
        await api("PATCH", `/api/admin/workers/${made.again}`,
                  { base_url: w.base_url, ...(w.kind === "ui" ? {} : { group: w.group ?? "" }) });
        const pid = setup.extra.profile_id;
        if (pid) await api("POST", `/api/admin/instances/${made.again}/assign`, { profile_id: pid });
        await api("POST", `/api/admin/setups/${made.id}/finished?worker_id=${made.again}`);
        // the new tools and environment take effect when it starts again — ask: a running worker restarts through a
        // rollout of its own profile (it finishes its job first); one that is not running is started
        close();
        const inst = (await api("GET", "/api/admin/instances")).find((x) => x.worker_id === made.again);
        const prof = pid || inst?.instance?.profile_id;
        if (!prof) return askStart(made.again, setup.worker.name, setup.worker.kind);
        const up = inst?.state === "online";
        const go = await formDialog(up ? t("Restart {name} now?", setup.worker) : t("Start {name} now?", setup.worker), [
          { name: "why", type: "pre", label: "", value: up
            ? t("The new worker tools and settings take effect when it starts again. A rollout of its own profile restarts it: it finishes its running job first, then it is verified.")
            : t("The new worker tools and settings take effect when it starts.") }],
          up ? t("Restart now") : t("Start now"));
        if (!go) return toast(t("Worker updated — restart it later: System → Rollout → Restart"));
        await act(() => up ? api("POST", "/api/admin/rollouts", { profile_id: prof, worker_ids: [made.again], window: 1, options: { restart: true } })
                           : api("POST", `/api/admin/instances/${made.again}/start`),
                  up ? t("Restarting it — see Rollout") : t("Worker updated and starting"), refresh);
        return;
      }
      if (setup.worker.group && !(await api("GET", "/api/admin/groups")).some((g) => g.name === setup.worker.group)) {
        await api("POST", "/api/admin/groups", { name: setup.worker.group });     // the group typed at setup (a new farm's first)
      }
      const w = await api("POST", "/api/admin/workers", setup.worker);
      await api("PUT", `/api/admin/instances/${w.id}`, setup.instance_config);
      const pid = setup.extra.profile_id;
      if (pid) {
        await api("POST", `/api/admin/instances/${w.id}/assign`, { profile_id: pid });
        await api("POST", `/api/admin/instances/${w.id}/start`);
      }
      await api("POST", `/api/admin/setups/${made.id}/finished?worker_id=${w.id}`);
      close();
      if (pid) toast(t("Worker added and starting — watch its badge in System → Rollout"));
      else askStart(w.id, setup.worker.name, setup.worker.kind);      // no profile picked at setup: ask now
    } catch (e) { toast(e.message, true); finish.disabled = false; poll(); }
  }
  poll();
}

// System → Rollout: the host check of a configured instance, through its agent.
export async function checkHost(workerId, label) {
  const r = await act(() => api("POST", `/api/admin/instances/${workerId}/check`));
  if (!r) return;
  const dlg = h("dialog", { class: "form wide" });
  const close = () => { dlg.close(); dlg.remove(); };
  dlg.append(h("h3", {}, t("Host check: {name}", { name: label })), checkTable(r.check),
    h("div", { class: "dialog-actions" }, button(t("Close"), close)));
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
}
