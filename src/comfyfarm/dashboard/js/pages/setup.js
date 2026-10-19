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
// again: { worker_id, spec, extra } of the worker's last setup — the same host set up once more (new worker tools,
// builds on or off); its name and instance stay, and the last step updates it instead of adding a worker.
export async function setupWorker(refresh, again = null) {
  // unfinished setups can be continued: the dialog may have been closed while the host ran the script
  const open = (await api("GET", "/api/admin/setups")).filter((x) => ["waiting", "failed", "reported"].includes(x.status));
  const pickRole = again ? { role: again.spec.role || "gpu" } : await formDialog(t("Set up a worker"), [
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
  const [groups, profiles, recent] = await Promise.all([api("GET", "/api/admin/groups"), api("GET", "/api/admin/profiles"),
                                                       api("GET", "/api/admin/setups")]);
  const last = again ? again.spec : recent[0]?.spec;
  const lastX = again ? again.extra : {};
  const pathOf = (k, d) => {
    if (k === "temp") {                                   // per instance: keep the template, not the last one's name
      const lt = last?.paths?.temp, li = last?.instance;
      return lt && li && lt.endsWith("/" + li) ? lt.slice(0, -li.length) + "{instance}" : d;
    }
    return (k === "art" ? last?.art : k === "local" ? last?.local : last?.paths?.[k]) ?? d;
  };
  const keep = again ? t("kept: setting a worker up again changes its host, not its identity") : "";
  const v = await formDialog(again ? t("Set up {name} again", { name: lastX.name }) : ui ? t("Set up the UI worker") : t("Set up a GPU worker"), [
    { name: "name", label: t("Worker name (shown in the dashboard)"), value: again ? lastX.name : ui ? "ui" : "", hint: keep },
    { name: "host", label: t("Host address"), value: lastX.host ?? "", placeholder: "10.0.0.21", hint: t("the address the router and the gateway reach this host at") },
    { name: "instance", label: t("Instance name"), value: again ? last.instance : ui ? "ui-1" : "gpu-1", hint: keep || t("one ComfyUI on this host; letters, digits, . _ -") },
    ...(ui ? [] : [
      { name: "gpu", label: t("GPU"), type: "select", value: last?.gpu && last.gpu !== "none" ? last.gpu : "amd",
        options: GPUS.map(([v, l]) => ({ value: v, label: t(l) })), hint: t("ComfyFarm checks the driver; it never installs one") },
      { name: "group", label: t("Group"), type: "select", value: again ? lastX.group ?? "" : groups[0]?.name ?? "",
        options: [{ value: "", label: t("(no group)") }, ...groups.map((g) => ({ value: g.name, label: g.name }))] }]),
    { name: "profile", label: t("Profile to run"), type: "select", value: "",
      options: [{ value: "", label: t("(assign later)") }, ...profiles.map((p) => ({ value: String(p.id), label: p.name }))],
      hint: ui ? t("UI profile: the core of the GPU workers, a CPU venv, nodes: comfyfarm_ui and comfyfarm_worker, flag --cpu, env COMFYFARM_ALLOW=<gateway and router addresses>") : "" },
    { name: "port", label: t("ComfyUI port"), type: "number", value: again ? last.port : ui ? 8189 : 8188,
      hint: ui ? t("not 8188 when it shares the host with the router") : "" },
    { name: "ssh_port", label: t("ssh port"), type: "number", value: lastX.ssh_port ?? 22 },
    { name: "account", label: t("Service account (its own, made by setup)"), value: last?.account ?? "comfyfarm",
      hint: t("not your login account: setup creates it to run ComfyUI, and only the router's key — restart, query, check — logs in as it") },
    { name: "uid", label: t("Service account uid"), type: "number", value: last?.uid ?? 47800,
      hint: t("the same number on every host: shared storage checks numbers, not names") },
    { name: "router_url", label: t("Router address as the worker sees it"), value: last?.router_url ?? "", placeholder: "http://10.0.0.5:8188",
      hint: t("http://<router address>:<router port> — the address the launcher fetches its render from") },
    ...PATHS.map(([k, label, d]) => ({ name: `path_${k}`, label: t(label), value: pathOf(k, d) })),
    { name: "mounts", label: t("NFS mounts (optional, one per line: server:/export /mountpoint [options])"), type: "textarea", rows: 2,
      value: "",                                            // per host: never carried over from another host's setup
      hint: t("written to /etc/fstab and mounted by setup; needs nfs-common on the host. Other storage: mount it yourself, the host check verifies it") },
    { name: "builds", type: "checkbox", value: !!last?.builds, text: t("Allow builds from the dashboard on this host"),
      label: t("Builds"),
      hint: t("the dashboard may then ask this host to build a recipe that setup installs here (it names one, never sends one); a build account owns the artifact store, the service account still only reads it. Needs internet, git and uv on the host") },
  ], t("Next"), { wide: true });
  if (!v) return;
  const inst = v.instance.trim();
  const body = {
    role: ui ? "ui" : "gpu", name: v.name.trim() || inst, host: v.host.trim(), instance: inst,
    gpu: ui ? "none" : v.gpu, group: ui ? null : v.group || null,
    profile_id: v.profile ? Number(v.profile) : null, port: v.port || 8188, ssh_port: v.ssh_port || 22,
    account: v.account.trim(), uid: v.uid || null, router_url: v.router_url.trim(),
    paths: Object.fromEntries(PATHS.map(([k]) => [k, v[`path_${k}`].trim().replace("{instance}", inst)])),
    mounts: v.mounts.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [source, target, options] = l.split(/\s+/);
      return options ? { source, target, options } : { source, target };
    }),
    builds: !!v.builds, worker_id: again ? again.worker_id : null,
  };
  const made = await act(() => api("POST", "/api/admin/setups", body));
  if (made) runSetup({ ...made, instance: inst, account: body.account, again: body.worker_id }, refresh);
}

// Step 2: get root on the host — the operator picks the way; then watch the report come in.
function runSetup(made, refresh) {
  let mode = "command", timer = null, closed = false;
  const status = h("div", { class: "section-head" });
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
    h("div", {}, runSsh));
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
  dlg.append(h("h3", {}, t("Set up {name}", { name: made.instance || "" })), choices, paneCmd, paneSsh, status, log, checks,
    h("div", { class: "dialog-actions" }, button(t("Close"), close), finish));
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
    if (["waiting", "running", "failed"].includes(setup.status) && !setup.expired) timer = setTimeout(poll, 2000);
  }

  // Step 3: the usual endpoints, in order — the same checks as doing it by hand.
  async function addWorker() {
    finish.disabled = true;
    try {
      if (made.again) {                                    // the same worker: its instance gets the new settings
        await api("PUT", `/api/admin/instances/${made.again}`, setup.instance_config);
        await api("POST", `/api/admin/setups/${made.id}/finished?worker_id=${made.again}`);
        toast(t("Worker updated — the new worker tools are used from its next start (Rollout → Render, or a rollout)"));
        return close();
      }
      const w = await api("POST", "/api/admin/workers", setup.worker);
      await api("PUT", `/api/admin/instances/${w.id}`, setup.instance_config);
      const pid = setup.extra.profile_id;
      if (pid) {
        await api("POST", `/api/admin/instances/${w.id}/assign`, { profile_id: pid });
        await api("POST", `/api/admin/instances/${w.id}/start`);
      }
      await api("POST", `/api/admin/setups/${made.id}/finished?worker_id=${w.id}`);
      toast(pid ? t("Worker added and starting — watch its badge in System → Rollout") : t("Worker added — assign a profile in System → Rollout"));
      close();
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
