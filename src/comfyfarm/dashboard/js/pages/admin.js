// System administration: workers, groups, users, statistics (admins only; the router enforces it as well).
import { TZ, act, api, button, dailyChart, dur, formDialog, h, hours, namedRow, noteInput, periodPicker, pill, progressOf,
         qs, setPill, setText, statCards, syncNote, syncRows, t, table, toast, when } from "../core.js";
import { locale } from "../i18n.js";
import { rollout } from "./rollout.js";
import { setupWorker } from "./setup.js";

const TABS = [["workers", "Workers"], ["groups", "Groups"], ["users", "Users"], ["rollout", "Rollout"], ["stats", "Statistics"],
              ["settings", "Settings"]];       // [route, English label]
const BACKENDS = ["cuda", "rocm", "xpu", "mps", "cpu"];
const OSES = [["posix", "Linux / macOS"], ["nt", "Windows"]];

export default {
  adminOnly: true,
  mount(root, ctx) {
    const sub = TABS.some(([k]) => k === ctx.sub) ? ctx.sub : "workers";
    root.append(h("nav", { class: "tabs" }, ...TABS.map(([k, label]) => h("a", { href: `#/admin/${k}`, class: k === sub ? "on" : "" }, t(label)))));
    const body = h("div");
    root.append(body);
    return { workers, groups, users, rollout, stats, settings }[sub](body, ctx);
  },
};

function reqText(req) {
  const parts = [];
  if (req?.backend) parts.push(req.backend.join("/"));
  if (req?.os) parts.push(req.os.map((o) => (o === "nt" ? t("Windows") : t("Linux/macOS"))).join("/"));
  if (req?.min_vram_gb) parts.push(t("≥ {n} GB VRAM", { n: req.min_vram_gb }));
  return parts.join(" · ") || t("any worker");
}
function reqFields(req = {}) {
  return [
    { name: "backend", label: t("Backend (none checked = any)"), type: "checkboxes", value: req.backend || [],
      options: BACKENDS.map((b) => ({ value: b, label: b })) },
    { name: "os", label: t("Operating system (none checked = any)"), type: "checkboxes", value: req.os || [],
      options: OSES.map(([v, l]) => ({ value: v, label: t(l) })) },
    { name: "min_vram_gb", label: t("Minimum VRAM (GB)"), type: "number", value: req.min_vram_gb ?? "", min: 0, step: "any" },
  ];
}
function reqFrom(v) {
  const req = {};
  if (v.backend.length) req.backend = v.backend;
  if (v.os.length) req.os = v.os;
  if (v.min_vram_gb != null) req.min_vram_gb = v.min_vram_gb;
  return req;
}
// Worker warnings come from the router in English; the fixed ones (and the fixed start of the others) are translated,
// node and model names after them stay as they are.
function warnText(x) {
  const whole = {
    "worker extension not installed: open to everyone on the network, no reconnect fix":
      t("worker extension not installed: open to everyone on the network, no reconnect fix"),
    "reconnect fix not active on this ComfyUI version: events may stop after a reconnect":
      t("reconnect fix not active on this ComfyUI version: events may stop after a reconnect"),
  }[x];
  if (whole) return whole;
  const starts = [["unsecured: accepts requests from any address — set COMFYFARM_ALLOW to the router and gateway",
                   t("unsecured: accepts requests from any address — set COMFYFARM_ALLOW to the router and gateway")],
                  ["differs from its group: ", t("differs from its group: ")],
                  ["custom nodes failed to import (missing on this worker, see its log): ",
                   t("custom nodes failed to import (missing on this worker, see its log): ")]];
  for (const [en, here] of starts) if (x.startsWith(en)) return here + x.slice(en.length);
  return x;
}
const head = (title, ...right) => h("div", { class: "section-head" }, h("h2", {}, title), ...right);

// ---- workers ------------------------------------------------------------------------------------------------------
function workers(root, { refresh }) {
  let groupList = [];
  const cols = ["name", "group", "state", "gpu", "job", "warnings", "note", "actions"];
  const tb = table([[t("Name")], [t("Group")], [t("State")], [t("GPU")], [t("Job")], [t("Warnings")], [t("Note")], ["", "actions"]]);
  const groupOptions = () => [{ value: "", label: t("(no group)") }, ...groupList.map((g) => ({ value: g.name, label: g.name }))];
  const patch = (id, body, msg) => act(() => api("PATCH", `/api/admin/workers/${id}`, body), msg, refresh);
  const add = button(t("Add worker"), async () => {
    const v = await formDialog(t("Add worker"), [
      { name: "name", label: t("Name") },
      { name: "base_url", label: t("ComfyUI URL"), placeholder: "http://host:8188" },
      { name: "group", label: t("Group"), type: "select", options: groupOptions(), value: groupList[0]?.name ?? "" },
    ], t("Add"));
    if (v) act(() => api("POST", "/api/admin/workers", v), t("Worker added — it appears online after its first health check"), refresh);
  }, { cls: "primary" });
  const guided = button(t("Set up a worker"), () => setupWorker(refresh), { cls: "primary",
    title: t("Turn an empty Linux host into a worker: setup script, host check, then the worker is added") });
  add.classList.remove("primary");
  root.append(h("section", {}, head(t("Workers"), guided, add), tb.el,
    h("p", { class: "muted small" }, t("Maintenance: no new jobs, the running one finishes. Disable: also no health checks. " +
      "Delete keeps the worker's history."))));

  function build() {
    const tr = namedRow(cols, { name: "nowrap", actions: "actions" });
    const id = () => tr._item.id;
    tr.sel = h("select", { onchange: () => patch(id(), { group: tr.sel.value }, t("Group changed")) });
    tr.uiNote = h("span", { class: "muted", hidden: true }, t("UI worker"));
    tr.c.group.append(tr.sel, tr.uiNote);
    tr.note = noteInput((v) => patch(id(), { note: v }, t("Note saved")));
    tr.c.note.append(tr.note);
    tr.maint = button("", () => patch(id(), { maintenance: !tr._item.maintenance }));
    tr.enable = button("", () => {
      const w = tr._item;
      if (w.enabled && !confirm(t("Disable {name}? It stops receiving jobs and is no longer health-checked.", w))) return;
      patch(id(), { enabled: !w.enabled });
    });
    const edit = button(t("Edit"), async () => {
      const w = tr._item;
      const v = await formDialog(t("Edit {name}", w), [{ name: "name", label: t("Name"), value: w.name },
        { name: "base_url", label: t("ComfyUI URL"), value: w.base_url, hint: t("changing it re-reads the worker's capabilities") }]);
      if (v) patch(id(), { name: v.name, ...(v.base_url !== w.base_url ? { base_url: v.base_url } : {}) }, t("Saved"));
    });
    const del = button(t("Delete"), () => {
      const w = tr._item;
      if (!confirm(w.job ? t("Delete {name}? Its running job will be reported as lost.", w) : t("Delete {name}?", w))) return;
      act(() => api("DELETE", `/api/admin/workers/${id()}`), t("Deleted"), refresh);
    }, { cls: "danger" });
    const again = button(t("Set up again"), async () => {
      const s = (await api("GET", `/api/admin/setups?worker_id=${id()}`)).find((x) => x.status === "finished");
      if (!s) return toast(t("this worker was not set up from the dashboard — Set up a worker, with another name"), true);
      setupWorker(refresh, { worker_id: id(), spec: s.spec, extra: s.extra });
    });
    tr.c.actions.append(tr.maint, tr.enable, again, edit, del);
    return tr;
  }
  function update(tr, w) {
    const c = tr.c;
    setText(c.name, w.name);
    c.name.title = w.base_url;
    const opts = groupOptions(), sig = opts.map((o) => o.value).join("|");
    if (tr.sel.dataset.v !== sig) {
      tr.sel.dataset.v = sig;
      tr.sel.replaceChildren(...opts.map((o) => h("option", { value: o.value }, o.label)));
    }
    if (document.activeElement !== tr.sel) tr.sel.value = w.group ?? "";
    tr.sel.hidden = w.kind === "ui"; tr.uiNote.hidden = w.kind !== "ui";      // a UI worker belongs to no group
    const st = !w.enabled ? "disabled" : w.state;
    const sig2 = `${st}|${w.maintenance}`;
    if (c.state.dataset.v !== sig2) {
      c.state.dataset.v = sig2;
      c.state.replaceChildren(pill(st), w.maintenance ? " " : "", w.maintenance ? pill("maintenance") : "");
    }
    setText(c.gpu, [w.gpu, w.vram_gb ? `${w.vram_gb} GB` : null, w.backend, w.comfyui ? `ComfyUI ${w.comfyui}` : null]
      .filter(Boolean).join(" · ") || "—");
    const frac = w.job ? progressOf(w.job.progress) : null;
    setText(c.job, w.job ? `${w.job.owner} · ${w.job.prompt_id.slice(0, 8)}${frac != null ? ` · ${Math.round(frac * 100)}%` : ""}`
      : w.state === "online" && w.enabled && !w.maintenance ? t("idle") : "");
    const ws = w.warnings.join("\n");
    if (c.warnings.dataset.v !== ws) {
      c.warnings.dataset.v = ws;
      c.warnings.replaceChildren(...(w.warnings.length ? [h("ul", { class: "warnings" }, ...w.warnings.map((x) => h("li", {}, warnText(x))))] : []));
    }
    syncNote(tr.note, w.note);
    setText(tr.maint, w.maintenance ? t("End maintenance") : t("Maintenance"));
    setText(tr.enable, w.enabled ? t("Disable") : t("Enable"));
  }
  return {
    poll: 3000,
    async refresh() {
      const [ws, gs] = await Promise.all([api("GET", "/api/admin/workers"), api("GET", "/api/admin/groups")]);
      groupList = gs;
      syncRows(tb.tbody, ws, (w) => w.id, build, update);
    },
  };
}

// ---- groups -------------------------------------------------------------------------------------------------------
function groups(root, { refresh }) {
  const tb = table([[t("Name")], [t("Runs on")], [t("Workers"), "num"], [t("Users"), "num"], [t("Unfinished jobs"), "num"], [t("Node list")], ["", "actions"]]);
  const add = button(t("New group"), async () => {
    const v = await formDialog(t("New worker group"), [{ name: "name", label: t("Name") }, ...reqFields()], t("Create"));
    if (v) act(() => api("POST", "/api/admin/groups", { name: v.name, requirements: reqFrom(v) }), t("Group created"), refresh);
  }, { cls: "primary" });
  root.append(h("section", {}, head(t("Groups"), add), tb.el,
    h("p", { class: "muted small" }, t("A group can only be deleted when it has no workers and no unfinished jobs. " +
      "Refresh the node list after installing or updating custom nodes on its workers."))));
  const cols = ["name", "req", "workers", "users", "jobs", "nodes", "actions"];
  function build() {
    const tr = namedRow(cols, { workers: "num", users: "num", jobs: "num", actions: "actions" });
    tr.c.actions.append(
      button(t("Edit"), async () => {
        const g = tr._item;
        const v = await formDialog(t("Edit {name}", g), [{ name: "name", label: t("Name"), value: g.name }, ...reqFields(g.requirements)]);
        if (v) act(() => api("PATCH", `/api/admin/groups/${g.id}`, { name: v.name, requirements: reqFrom(v) }), t("Saved"), refresh);
      }),
      button(t("Refresh nodes"), () => act(() => api("POST", `/api/admin/groups/${encodeURIComponent(tr._item.name)}/object_info/refresh`),
        t("Node list refreshed"), refresh)),
      button(t("Delete"), () => {
        if (confirm(t("Delete group {name}?", tr._item))) act(() => api("DELETE", `/api/admin/groups/${tr._item.id}`), t("Deleted"), refresh);
      }, { cls: "danger" }));
    return tr;
  }
  function update(tr, g) {
    setText(tr.c.name, g.name); setText(tr.c.req, reqText(g.requirements));
    setText(tr.c.workers, g.workers); setText(tr.c.users, g.users); setText(tr.c.jobs, g.active_jobs);
    setText(tr.c.nodes, g.nodes_at ? t("fetched {time}", { time: when(g.nodes_at) }) : t("not fetched yet"));
  }
  return { async refresh() { syncRows(tb.tbody, await api("GET", "/api/admin/groups"), (g) => g.id, build, update); } };
}

// ---- users --------------------------------------------------------------------------------------------------------
function users(root, { refresh }) {
  let groupList = [];
  const tb = table([[t("User")], [t("Role")], [t("Sign-in")], [t("Worker groups")], [t("Status")], [t("Last job")], ["", "actions"]]);
  const groupField = (value = []) => ({ name: "groups", label: t("Worker groups this user may use"), type: "checkboxes",
    value: value.map(String), options: groupList.map((g) => ({ value: String(g.id), label: g.name })),
    hint: t("administrators may use every group regardless") });
  const add = button(t("New user"), async () => {
    const v = await formDialog(t("New user"), [
      { name: "username", label: t("User name") },
      { name: "password", label: t("Initial password"), type: "password", autocomplete: "new-password", hint: t("at least 8 characters") },
      { name: "admin", label: t("Role"), type: "checkbox", text: t("administrator") }, groupField()], t("Create"));
    if (v) act(() => api("POST", "/api/admin/users", { ...v, groups: v.groups.map(Number) }), t("User created"), refresh);
  }, { cls: "primary" });
  root.append(h("section", {}, head(t("Users"), add), tb.el,
    h("p", { class: "muted small" }, t("Disabling a user or setting a new password signs them out within 30 seconds. " +
      "Their jobs and history stay. The last active administrator cannot be demoted, disabled or deleted."))));
  const cols = ["user", "role", "signin", "groups", "status", "last", "actions"];
  function build() {
    const tr = namedRow(cols, { last: "num", actions: "actions" });
    const name = () => tr._item.username;
    const patch = (body, msg) => act(() => api("PATCH", `/api/admin/users/${encodeURIComponent(name())}`, body), msg, refresh);
    tr.toggle = button("", () => {
      const u = tr._item;
      if (!u.disabled && !confirm(t("Disable {username}? They are signed out and cannot log in.", u))) return;
      patch({ disabled: !u.disabled });
    });
    tr.c.actions.append(
      button(t("Edit"), async () => {
        const u = tr._item;
        const v = await formDialog(t("Edit {name}", { name: u.username }), [
          { name: "admin", label: t("Role"), type: "checkbox", text: t("administrator"), value: u.admin },
          groupField(u.groups),
          { name: "password", label: t("New password"), type: "password", autocomplete: "new-password",
            hint: u.local ? t("leave empty to keep the current one") : t("this account signs in through another provider") }]);
        if (!v) return;
        const body = { admin: v.admin, groups: v.groups.map(Number) };
        if (v.password) body.password = v.password;
        patch(body, t("Saved"));
      }),
      tr.toggle,
      button(t("Delete"), () => {
        if (confirm(t("Delete the account {name}? Their jobs and history stay.", { name: name() })))
          act(() => api("DELETE", `/api/admin/users/${encodeURIComponent(name())}`), t("Deleted"), refresh);
      }, { cls: "danger" }));
    return tr;
  }
  function update(tr, u) {
    setText(tr.c.user, u.username);
    setText(tr.c.role, u.admin ? t("admin") : t("user"));
    setText(tr.c.signin, u.local ? t("password") : t("external"));
    const names = u.groups.map((id) => groupList.find((g) => g.id === id)?.name ?? `#${id}`);
    setText(tr.c.groups, u.admin ? t("all (admin)") : names.join(", ") || t("none — cannot submit"));
    tr.c.groups.className = !u.admin && !names.length ? "warn" : "";
    setPill(tr.c.status, u.disabled ? "disabled" : "online", u.disabled ? t("disabled") : t("active"));
    setText(tr.c.last, u.last_job_at ? when(u.last_job_at) : "—");
    setText(tr.toggle, u.disabled ? t("Enable") : t("Disable"));
  }
  return {
    async refresh() {
      const [us, gs] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/groups")]);
      groupList = gs;
      syncRows(tb.tbody, us, (u) => u.username, build, update);
    },
  };
}

// ---- statistics ---------------------------------------------------------------------------------------------------
function stats(root, { refresh }) {
  let period = JSON.parse(localStorage.getItem("comfyfarm.stats.period") || '{"days":7}');
  const out = h("div");
  root.append(h("section", {}, head(t("Statistics"), periodPicker(period, (p) => {
    period = p; localStorage.setItem("comfyfarm.stats.period", JSON.stringify(p)); refresh();
  })), out));
  const tbl = (title, rows, first, extra = []) => {
    const tb = table([[first], [t("Jobs"), "num"], [t("Completed"), "num"], [t("Failed"), "num"], [t("Cancelled"), "num"], [t("GPU time"), "num"],
                     [t("Avg wait"), "num"], [t("Avg run"), "num"], ...extra.map(([l]) => [l, "num"])]);
    for (const r of rows) {
      tb.tbody.append(h("tr", {}, h("td", {}, r._label), ...[r.jobs, r.completed, r.failed, r.cancelled].map((n) => h("td", { class: "num" }, String(n))),
        h("td", { class: "num" }, hours(r.gpu_seconds)), h("td", { class: "num" }, dur(r.avg_wait_seconds)),
        h("td", { class: "num" }, dur(r.avg_run_seconds)), ...extra.map(([, f]) => h("td", { class: "num" }, f(r)))));
    }
    return h("div", { class: "block" }, h("h3", {}, title), rows.length ? tb.el : h("p", { class: "muted" }, t("No jobs in this period.")));
  };
  return {
    async refresh() {
      const s = await api("GET", `/api/admin/stats?${qs({ ...period, tz: TZ })}`);
      const range = `${new Date(s.since).toLocaleString(locale())} – ${new Date(s.until).toLocaleString(locale())}`;
      out.replaceChildren(
        h("p", { class: "muted small" }, range),
        statCards(s.total), dailyChart(s.daily),
        tbl(t("By group"), s.groups.map((g) => ({ ...g, _label: g.name })), t("Group")),
        tbl(t("By user"), s.users.map((u) => ({ ...u, _label: u.user })), t("User")),
        tbl(t("By worker"), s.workers.map((w) => ({ ...w, _label: w.name })), t("Worker"),
          [[t("Busy"), (w) => (w.utilization == null ? "—" : w.utilization > 0 && w.utilization < 0.001 ? "<0.1%" : `${(w.utilization * 100).toFixed(1)}%`)]]),
        h("p", { class: "muted small" }, t("Jobs count in the period they finished. GPU time = worker held the job (dispatch → finish, " +
          "model loading included). Busy = share of the period the worker held any job.")));
    },
  };
}

// ---- site settings ------------------------------------------------------------------------------------------------
const SETTING_LABELS = { login_user_list: "Pick the user name from a list on the login page" };

function settings(root, { refresh }) {
  const box = h("div", { class: "panel stack" });
  root.append(h("section", {}, head(t("Settings")), box));
  let sig = "";
  return {
    async refresh() {
      const all = await api("GET", "/api/admin/settings");
      const next = JSON.stringify(all);
      if (next === sig) return;
      sig = next;
      box.replaceChildren(...Object.entries(all).map(([key, st]) => h("div", {},
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.value === true, onchange: (e) =>
          act(() => api("PUT", "/api/admin/settings", { [key]: e.target.checked }), t("Saved"), refresh) }),
          ` ${SETTING_LABELS[key] ? t(SETTING_LABELS[key]) : key}`),
        h("div", { class: "muted small" }, t(st.description)))));
    },
  };
}
