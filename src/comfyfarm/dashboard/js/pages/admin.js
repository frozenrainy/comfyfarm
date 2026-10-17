// System administration: workers, groups, users, statistics (admins only; the router enforces it as well).
import { TZ, act, api, button, dailyChart, dur, formDialog, h, hours, namedRow, noteInput, periodPicker, pill, progressOf,
         qs, setPill, setText, statCards, syncNote, syncRows, table, when } from "../core.js";

const TABS = [["workers", "Workers"], ["groups", "Groups"], ["users", "Users"], ["stats", "Statistics"]];
const BACKENDS = ["cuda", "rocm", "xpu", "mps", "cpu"];
const OSES = [["posix", "Linux / macOS"], ["nt", "Windows"]];

export default {
  adminOnly: true,
  mount(root, ctx) {
    const sub = TABS.some(([k]) => k === ctx.sub) ? ctx.sub : "workers";
    root.append(h("nav", { class: "tabs" }, ...TABS.map(([k, label]) => h("a", { href: `#/admin/${k}`, class: k === sub ? "on" : "" }, label))));
    const body = h("div");
    root.append(body);
    return { workers, groups, users, stats }[sub](body, ctx);
  },
};

function reqText(req) {
  const parts = [];
  if (req?.backend) parts.push(req.backend.join("/"));
  if (req?.os) parts.push(req.os.map((o) => (o === "nt" ? "Windows" : "Linux/macOS")).join("/"));
  if (req?.min_vram_gb) parts.push(`≥ ${req.min_vram_gb} GB VRAM`);
  return parts.join(" · ") || "any worker";
}
function reqFields(req = {}) {
  return [
    { name: "backend", label: "Backend (none checked = any)", type: "checkboxes", value: req.backend || [],
      options: BACKENDS.map((b) => ({ value: b, label: b })) },
    { name: "os", label: "Operating system (none checked = any)", type: "checkboxes", value: req.os || [],
      options: OSES.map(([v, l]) => ({ value: v, label: l })) },
    { name: "min_vram_gb", label: "Minimum VRAM (GB)", type: "number", value: req.min_vram_gb ?? "", min: 0, step: "any" },
  ];
}
function reqFrom(v) {
  const req = {};
  if (v.backend.length) req.backend = v.backend;
  if (v.os.length) req.os = v.os;
  if (v.min_vram_gb != null) req.min_vram_gb = v.min_vram_gb;
  return req;
}
const head = (title, ...right) => h("div", { class: "section-head" }, h("h2", {}, title), ...right);

// ---- workers ------------------------------------------------------------------------------------------------------
function workers(root, { refresh }) {
  let groupList = [];
  const cols = ["name", "group", "state", "gpu", "job", "warnings", "note", "actions"];
  const t = table([["Name"], ["Group"], ["State"], ["GPU"], ["Job"], ["Warnings"], ["Note"], ["", "actions"]]);
  const groupOptions = () => [{ value: "", label: "(no group)" }, ...groupList.map((g) => ({ value: g.name, label: g.name }))];
  const patch = (id, body, msg) => act(() => api("PATCH", `/api/admin/workers/${id}`, body), msg, refresh);
  const add = button("Add worker", async () => {
    const v = await formDialog("Add worker", [
      { name: "name", label: "Name" },
      { name: "base_url", label: "ComfyUI URL", placeholder: "http://host:8188" },
      { name: "group", label: "Group", type: "select", options: groupOptions(), value: groupList[0]?.name ?? "" },
    ], "Add");
    if (v) act(() => api("POST", "/api/admin/workers", v), "Worker added — it appears online after its first health check", refresh);
  }, { cls: "primary" });
  root.append(h("section", {}, head("Workers", add), t.el,
    h("p", { class: "muted small" }, "Maintenance: no new jobs, the running one finishes. Disable: also no health checks. " +
      "Delete keeps the worker's history.")));

  function build() {
    const tr = namedRow(cols, { actions: "actions" });
    const id = () => tr._item.id;
    tr.sel = h("select", { onchange: () => patch(id(), { group: tr.sel.value }, "Group changed") });
    tr.c.group.append(tr.sel);
    tr.note = noteInput((v) => patch(id(), { note: v }, "Note saved"));
    tr.c.note.append(tr.note);
    tr.maint = button("", () => patch(id(), { maintenance: !tr._item.maintenance }));
    tr.enable = button("", () => {
      const w = tr._item;
      if (w.enabled && !confirm(`Disable ${w.name}? It stops receiving jobs and is no longer health-checked.`)) return;
      patch(id(), { enabled: !w.enabled });
    });
    const edit = button("Edit", async () => {
      const w = tr._item;
      const v = await formDialog(`Edit ${w.name}`, [{ name: "name", label: "Name", value: w.name },
        { name: "base_url", label: "ComfyUI URL", value: w.base_url, hint: "changing it re-reads the worker's capabilities" }]);
      if (v) patch(id(), { name: v.name, ...(v.base_url !== w.base_url ? { base_url: v.base_url } : {}) }, "Saved");
    });
    const del = button("Delete", () => {
      const w = tr._item;
      if (!confirm(`Delete ${w.name}?${w.job ? " Its running job will be reported as lost." : ""}`)) return;
      act(() => api("DELETE", `/api/admin/workers/${id()}`), "Deleted", refresh);
    }, { cls: "danger" });
    tr.c.actions.append(tr.maint, tr.enable, edit, del);
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
      : w.state === "online" && w.enabled && !w.maintenance ? "idle" : "");
    const ws = w.warnings.join("\n");
    if (c.warnings.dataset.v !== ws) {
      c.warnings.dataset.v = ws;
      c.warnings.replaceChildren(...(w.warnings.length ? [h("ul", { class: "warnings" }, ...w.warnings.map((x) => h("li", {}, x)))] : []));
    }
    syncNote(tr.note, w.note);
    setText(tr.maint, w.maintenance ? "End maintenance" : "Maintenance");
    setText(tr.enable, w.enabled ? "Disable" : "Enable");
  }
  return {
    poll: 3000,
    async refresh() {
      const [ws, gs] = await Promise.all([api("GET", "/api/admin/workers"), api("GET", "/api/admin/groups")]);
      groupList = gs;
      syncRows(t.tbody, ws, (w) => w.id, build, update);
    },
  };
}

// ---- groups -------------------------------------------------------------------------------------------------------
function groups(root, { refresh }) {
  const t = table([["Name"], ["Runs on"], ["Workers", "num"], ["Users", "num"], ["Unfinished jobs", "num"], ["Node list"], ["", "actions"]]);
  const add = button("New group", async () => {
    const v = await formDialog("New worker group", [{ name: "name", label: "Name" }, ...reqFields()], "Create");
    if (v) act(() => api("POST", "/api/admin/groups", { name: v.name, requirements: reqFrom(v) }), "Group created", refresh);
  }, { cls: "primary" });
  root.append(h("section", {}, head("Groups", add), t.el,
    h("p", { class: "muted small" }, "A group can only be deleted when it has no workers and no unfinished jobs. " +
      "Refresh the node list after installing or updating custom nodes on its workers.")));
  const cols = ["name", "req", "workers", "users", "jobs", "nodes", "actions"];
  function build() {
    const tr = namedRow(cols, { workers: "num", users: "num", jobs: "num", actions: "actions" });
    tr.c.actions.append(
      button("Edit", async () => {
        const g = tr._item;
        const v = await formDialog(`Edit ${g.name}`, [{ name: "name", label: "Name", value: g.name }, ...reqFields(g.requirements)]);
        if (v) act(() => api("PATCH", `/api/admin/groups/${g.id}`, { name: v.name, requirements: reqFrom(v) }), "Saved", refresh);
      }),
      button("Refresh nodes", () => act(() => api("POST", `/api/admin/groups/${encodeURIComponent(tr._item.name)}/object_info/refresh`),
        "Node list refreshed", refresh)),
      button("Delete", () => {
        if (confirm(`Delete group ${tr._item.name}?`)) act(() => api("DELETE", `/api/admin/groups/${tr._item.id}`), "Deleted", refresh);
      }, { cls: "danger" }));
    return tr;
  }
  function update(tr, g) {
    setText(tr.c.name, g.name); setText(tr.c.req, reqText(g.requirements));
    setText(tr.c.workers, g.workers); setText(tr.c.users, g.users); setText(tr.c.jobs, g.active_jobs);
    setText(tr.c.nodes, g.nodes_at ? `fetched ${when(g.nodes_at)}` : "not fetched yet");
  }
  return { async refresh() { syncRows(t.tbody, await api("GET", "/api/admin/groups"), (g) => g.id, build, update); } };
}

// ---- users --------------------------------------------------------------------------------------------------------
function users(root, { refresh }) {
  let groupList = [];
  const t = table([["User"], ["Role"], ["Sign-in"], ["Worker groups"], ["Status"], ["Last job"], ["", "actions"]]);
  const groupField = (value = []) => ({ name: "groups", label: "Worker groups this user may use", type: "checkboxes",
    value: value.map(String), options: groupList.map((g) => ({ value: String(g.id), label: g.name })),
    hint: "administrators may use every group regardless" });
  const add = button("New user", async () => {
    const v = await formDialog("New user", [
      { name: "username", label: "User name" },
      { name: "password", label: "Initial password", type: "password", autocomplete: "new-password", hint: "at least 8 characters" },
      { name: "admin", label: "Role", type: "checkbox", text: "administrator" }, groupField()], "Create");
    if (v) act(() => api("POST", "/api/admin/users", { ...v, groups: v.groups.map(Number) }), "User created", refresh);
  }, { cls: "primary" });
  root.append(h("section", {}, head("Users", add), t.el,
    h("p", { class: "muted small" }, "Disabling a user or setting a new password signs them out within 30 seconds. " +
      "Their jobs and history stay. The last active administrator cannot be demoted, disabled or deleted.")));
  const cols = ["user", "role", "signin", "groups", "status", "last", "actions"];
  function build() {
    const tr = namedRow(cols, { last: "num", actions: "actions" });
    const name = () => tr._item.username;
    const patch = (body, msg) => act(() => api("PATCH", `/api/admin/users/${encodeURIComponent(name())}`, body), msg, refresh);
    tr.toggle = button("", () => {
      const u = tr._item;
      if (!u.disabled && !confirm(`Disable ${u.username}? They are signed out and cannot log in.`)) return;
      patch({ disabled: !u.disabled });
    });
    tr.c.actions.append(
      button("Edit", async () => {
        const u = tr._item;
        const v = await formDialog(`Edit ${u.username}`, [
          { name: "admin", label: "Role", type: "checkbox", text: "administrator", value: u.admin },
          groupField(u.groups),
          { name: "password", label: "New password", type: "password", autocomplete: "new-password",
            hint: u.local ? "leave empty to keep the current one" : "this account signs in through another provider" }]);
        if (!v) return;
        const body = { admin: v.admin, groups: v.groups.map(Number) };
        if (v.password) body.password = v.password;
        patch(body, "Saved");
      }),
      tr.toggle,
      button("Delete", () => {
        if (confirm(`Delete the account ${name()}? Their jobs and history stay.`))
          act(() => api("DELETE", `/api/admin/users/${encodeURIComponent(name())}`), "Deleted", refresh);
      }, { cls: "danger" }));
    return tr;
  }
  function update(tr, u) {
    setText(tr.c.user, u.username);
    setText(tr.c.role, u.admin ? "admin" : "user");
    setText(tr.c.signin, u.local ? "password" : "external");
    const names = u.groups.map((id) => groupList.find((g) => g.id === id)?.name ?? `#${id}`);
    setText(tr.c.groups, u.admin ? "all (admin)" : names.join(", ") || "none — cannot submit");
    tr.c.groups.className = !u.admin && !names.length ? "warn" : "";
    setPill(tr.c.status, u.disabled ? "disabled" : "online", u.disabled ? "disabled" : "active");
    setText(tr.c.last, u.last_job_at ? when(u.last_job_at) : "—");
    setText(tr.toggle, u.disabled ? "Enable" : "Disable");
  }
  return {
    async refresh() {
      const [us, gs] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/groups")]);
      groupList = gs;
      syncRows(t.tbody, us, (u) => u.username, build, update);
    },
  };
}

// ---- statistics ---------------------------------------------------------------------------------------------------
function stats(root, { refresh }) {
  let period = JSON.parse(localStorage.getItem("comfyfarm.stats.period") || '{"days":7}');
  const out = h("div");
  root.append(h("section", {}, head("Statistics", periodPicker(period, (p) => {
    period = p; localStorage.setItem("comfyfarm.stats.period", JSON.stringify(p)); refresh();
  })), out));
  const tbl = (title, rows, first, extra = []) => {
    const t = table([[first], ["Jobs", "num"], ["Completed", "num"], ["Failed", "num"], ["Cancelled", "num"], ["GPU time", "num"],
                     ["Avg wait", "num"], ["Avg run", "num"], ...extra.map(([l]) => [l, "num"])]);
    for (const r of rows) {
      t.tbody.append(h("tr", {}, h("td", {}, r._label), ...[r.jobs, r.completed, r.failed, r.cancelled].map((n) => h("td", { class: "num" }, String(n))),
        h("td", { class: "num" }, hours(r.gpu_seconds)), h("td", { class: "num" }, dur(r.avg_wait_seconds)),
        h("td", { class: "num" }, dur(r.avg_run_seconds)), ...extra.map(([, f]) => h("td", { class: "num" }, f(r)))));
    }
    return h("div", { class: "block" }, h("h3", {}, title), rows.length ? t.el : h("p", { class: "muted" }, "No jobs in this period."));
  };
  return {
    async refresh() {
      const s = await api("GET", `/api/admin/stats?${qs({ ...period, tz: TZ })}`);
      const range = `${new Date(s.since).toLocaleString()} – ${new Date(s.until).toLocaleString()}`;
      out.replaceChildren(
        h("p", { class: "muted small" }, range),
        statCards(s.total), dailyChart(s.daily),
        tbl("By group", s.groups.map((g) => ({ ...g, _label: g.name })), "Group"),
        tbl("By user", s.users.map((u) => ({ ...u, _label: u.user })), "User"),
        tbl("By worker", s.workers.map((w) => ({ ...w, _label: w.name })), "Worker",
          [["Busy", (w) => (w.utilization == null ? "—" : w.utilization > 0 && w.utilization < 0.001 ? "<0.1%" : `${(w.utilization * 100).toFixed(1)}%`)]]),
        h("p", { class: "muted small" }, "Jobs count in the period they finished. GPU time = worker held the job (dispatch → finish, " +
          "model loading included). Busy = share of the period the worker held any job."));
    },
  };
}
