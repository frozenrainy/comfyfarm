// ComfyFarm dashboard. No framework, no build step (D-32). Polls /api/farm/overview and updates rows in place by key,
// so a note being edited or a button under the pointer survives the refresh.
const $ = (s) => document.querySelector(s);
const POLL_VISIBLE = 2000, POLL_HIDDEN = 15000;
const DASH = "/comfyfarm/dashboard/";

let scope = localStorage.getItem("comfyfarm.scope") === "all" ? "all" : "mine";
let groupFilter = localStorage.getItem("comfyfarm.group") || "";
let data = null, lastOk = null, clockSkew = 0, timer = null;

// ---- helpers --------------------------------------------------------------------------------------------------
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v);
  }
  for (const c of children) if (c != null) el.append(c);
  return el;
}
function setText(el, text) { text = text ?? ""; if (el.textContent !== String(text)) el.textContent = text; }
function pill(el, state, label) {
  el.replaceChildren(h("span", { class: `pill s-${state}` }, label ?? state.replace("_", " ")));
}
function now() { return Date.now() + clockSkew; }
function ago(iso) { return iso ? (now() - Date.parse(iso)) / 1000 : null; }
function dur(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
function groupName(id) {
  if (id == null) return "(any)";
  return data?.groups.find((g) => g.id === id)?.name ?? `#${id}`;
}
function progressOf(p) {
  const s = p?.state;
  if (!s || typeof s !== "object") return null;
  if (typeof s.max === "number" && s.max > 0) return s.value / s.max;
  const nodes = Object.values(s).filter((n) => n && typeof n === "object");     // per-node progress_state
  if (!nodes.length) return null;
  const done = nodes.reduce((a, n) => a + (n.state === "finished" ? 1 : n.max ? (n.value || 0) / n.max : 0), 0);
  return done / nodes.length;
}
function bar(frac) {
  if (frac == null) return "";
  const pct = Math.round(Math.min(1, Math.max(0, frac)) * 100);
  const i = h("i"); i.style.width = pct + "%";
  return h("span", {}, h("span", { class: "bar" }, i), ` ${pct}%`);
}
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = `/comfyfarm/login?next=${DASH}`; throw new Error("not logged in"); }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); const d = j.detail ?? j.error; if (typeof d === "string") msg = d; } catch {}
    throw new Error(msg);
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : null;
}
async function act(fn, okMsg) {
  try { const res = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(res) : okMsg); }
  catch (e) { toast(e.message); }
  refresh();
}

// Reconcile <tbody> rows by key: drop the gone ones first, then reuse and reorder only when needed. (Dropping last
// would make every row below a finished job "out of place" and get moved — and a moved row loses keyboard focus.)
function syncRows(tbody, items, key, build, update) {
  const keep = new Set(items.map((it) => String(key(it))));
  for (const tr of [...tbody.children]) if (!keep.has(tr.dataset.key)) tr.remove();
  const existing = new Map([...tbody.children].map((tr) => [tr.dataset.key, tr]));
  let prev = null;
  for (const it of items) {
    const k = String(key(it));
    let tr = existing.get(k);
    if (!tr) { tr = build(); tr.dataset.key = k; }
    tr._item = it;
    update(tr, it);
    const want = prev ? prev.nextSibling : tbody.firstChild;
    if (tr !== want) tbody.insertBefore(tr, want);
    prev = tr;
  }
}
function noteInput(save) {
  const inp = h("input", { class: "note", placeholder: "note", maxlength: "500" });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); if (e.key === "Escape") { inp.value = inp.dataset.saved || ""; inp.blur(); } });
  inp.addEventListener("change", () => save(inp));
  return inp;
}
function syncNote(inp, value) {
  inp.dataset.saved = value || "";
  if (document.activeElement !== inp) inp.value = value || "";
}

// ---- groups ---------------------------------------------------------------------------------------------------
function renderGroups() {
  const cards = data.groups.map((g) => {
    const hh = g.health || {};
    const stall = hh.dispatch_idle_seconds;
    const stallCls = stall == null ? "" : stall >= 300 ? "alert" : stall >= 60 ? "caution" : "";
    const row = (k, v, cls = "") => [h("dt", {}, k), h("dd", cls ? { class: cls } : {}, v)];
    const s = g.last_24h;
    return h("div", { class: "card" },
      h("h3", {}, h("span", {}, g.name), h("span", { class: "muted" }, `${g.online}/${g.workers} online`)),
      h("dl", {},
        ...row("Idle workers", String(hh.idle_workers ?? "—")),
        ...row("Pending", String(hh.pending ?? "—")),
        ...row("Paused", String(hh.paused ?? "—")),
        ...row("Running", String(hh.running ?? "—")),
        ...row("Dispatch stalled", stall == null ? "—" : dur(stall), stallCls),
        ...row("Longest running", dur(hh.running_longest_seconds)),
        ...(hh.unsatisfiable_pending ? row("Unsatisfiable", `${hh.unsatisfiable_pending} job(s): no worker meets the requirements`, "alert") : []),
        ...row("24 h", `${s.completed} done · ${s.failed} failed · ${s.cancelled} cancelled`),
        ...row("Avg wait / run", `${dur(s.avg_wait_seconds)} / ${dur(s.avg_run_seconds)}`),
      ));
  });
  $("#groups").replaceChildren(...cards);
  if (!cards.length) $("#groups").append(h("p", { class: "muted" }, "No groups yet. An admin creates them with POST /api/admin/groups."));
  const ug = data.ungrouped_unsatisfiable_pending;
  $("#ungrouped").hidden = !ug;
  setText($("#ungrouped"), ug ? `${ug} ungrouped job(s) wait for capabilities no group has.` : "");

  const sel = $("#group-filter");
  const want = ["", ...data.groups.map((g) => String(g.id))];
  if ([...sel.options].map((o) => o.value).join() !== want.join()) {
    sel.replaceChildren(h("option", { value: "" }, "all groups"), ...data.groups.map((g) => h("option", { value: String(g.id) }, g.name)));
  }
  sel.value = want.includes(groupFilter) ? groupFilter : "";
}

// ---- queue ----------------------------------------------------------------------------------------------------
function visibleQueue() {
  return data.queue.filter((j) => !groupFilter || String(j.group_id) === groupFilter);
}
function buildQueueRow() {
  const tr = h("tr");
  const cells = ["place", "status", "owner", "group", "worker", "progress", "time", "note", "actions"]
    .map((c) => h("td", { class: c === "place" || c === "time" ? "num" : c === "actions" ? "actions" : "" }));
  tr.append(...cells);
  tr._c = Object.fromEntries(cells.map((td, i) => [["place", "status", "owner", "group", "worker", "progress", "time", "note", "actions"][i], td]));
  const pid = () => tr._item.prompt_id;
  tr._note = noteInput((inp) => act(() => api("PUT", `/api/farm/jobs/${pid()}/note`, { note: inp.value }), "Note saved"));
  tr._c.note.append(tr._note);
  const b = (label, title, fn, cls = "") => h("button", { title, class: cls, onclick: fn }, label);
  tr._btn = {
    top: b("⤒", "Run next", () => act(() => api("POST", `/api/farm/jobs/${pid()}/move`, { to: "top" }))),
    up: b("↑", "Move up", () => move(tr, -1)),
    down: b("↓", "Move down", () => move(tr, +1)),
    bottom: b("⤓", "Move to the end", () => act(() => api("POST", `/api/farm/jobs/${pid()}/move`, { to: "bottom" }))),
    pause: b("Pause", "Keep it in the queue but skip it", () => act(() => api("POST", `/api/farm/jobs/${pid()}/pause`))),
    resume: b("Resume", "", () => act(() => api("POST", `/api/farm/jobs/${pid()}/resume`))),
    cancel: b("Cancel", "", () => {
      const j = tr._item;
      if (["dispatching", "running"].includes(j.status) && !confirm("Interrupt this running job?")) return;
      act(() => api("POST", `/api/farm/jobs/${pid()}/cancel`));
    }, "danger"),
  };
  tr._c.actions.append(...Object.values(tr._btn));
  return tr;
}
function move(tr, dir) {
  const queued = visibleQueue().filter((j) => j.place != null);
  const i = queued.findIndex((j) => j.prompt_id === tr._item.prompt_id);
  const other = queued[i + dir];
  if (!other) return;
  act(() => api("POST", `/api/farm/jobs/${tr._item.prompt_id}/move`, dir < 0 ? { before: other.prompt_id } : { after: other.prompt_id }));
}
function updateQueueRow(tr, j) {
  const c = tr._c, admin = data.me.admin, queued = j.place != null;
  setText(c.place, queued ? j.place : "");
  pill(c.status, j.status);
  setText(c.owner, j.owner);
  setText(c.group, groupName(j.group_id));
  setText(c.worker, j.worker || "");
  const frac = j.status === "running" ? progressOf(j.progress) : null;
  const key = frac == null ? "" : Math.round(frac * 100);
  if (c.progress.dataset.v !== String(key)) { c.progress.dataset.v = key; c.progress.replaceChildren(bar(frac)); }
  setText(c.time, dur(ago(queued ? j.created_at : j.started_at)));
  syncNote(tr._note, j.note);
  const show = (k, on) => { tr._btn[k].hidden = !on; };
  show("top", admin && queued); show("up", admin && queued); show("down", admin && queued); show("bottom", admin && queued);
  show("pause", j.status === "pending"); show("resume", j.status === "paused");
  show("cancel", j.status !== "cancelling");
}
function renderQueue() {
  const items = visibleQueue();
  syncRows($("#queue tbody"), items, (j) => j.key, buildQueueRow, updateQueueRow);
  $("#queue-empty").hidden = items.length > 0;
}

// ---- workers --------------------------------------------------------------------------------------------------
function buildWorkerRow() {
  const names = ["name", "group", "state", "gpu", "job", "warnings", "note", "actions"];
  const cells = names.map((n) => h("td", { class: n === "note" ? "admin-only" : n === "actions" ? "actions admin-only" : "" }));
  const tr = h("tr", {}, ...cells);
  tr._c = Object.fromEntries(cells.map((td, i) => [names[i], td]));
  const id = () => tr._item.id;
  tr._note = noteInput((inp) => act(() => api("PATCH", `/api/admin/workers/${id()}`, { note: inp.value }), "Note saved"));
  tr._c.note.append(tr._note);
  tr._maint = h("button", { onclick: () => {
    const w = tr._item;
    act(() => api("PATCH", `/api/admin/workers/${id()}`, { maintenance: !w.maintenance }),
        w.maintenance ? "Back in service" : "Maintenance: no new jobs; the running one finishes");
  } });
  tr._enable = h("button", { class: "danger", onclick: () => {
    const w = tr._item;
    if (w.enabled && !confirm(`Disable ${w.name}? It stops receiving jobs and is no longer health-checked.`)) return;
    act(() => api("PATCH", `/api/admin/workers/${id()}`, { enabled: !w.enabled }));
  } });
  tr._c.actions.append(tr._maint, " ", tr._enable);
  return tr;
}
function updateWorkerRow(tr, w) {
  const c = tr._c;
  setText(c.name, w.name);
  setText(c.group, w.group ?? "—");
  const st = !w.enabled ? "disabled" : w.state;
  const pills = [h("span", { class: `pill s-${st}` }, st.replace("_", " "))];
  if (w.maintenance) pills.push(" ", h("span", { class: "pill s-maintenance" }, "maintenance"));
  const sig = `${st}|${w.maintenance}`;
  if (c.state.dataset.v !== sig) { c.state.dataset.v = sig; c.state.replaceChildren(...pills); }
  setText(c.gpu, [w.gpu, w.vram_gb ? `${w.vram_gb} GB` : null, w.backend].filter(Boolean).join(" · ") || "—");
  if (w.job) {
    const frac = progressOf(w.job.progress);
    setText(c.job, `${w.job.owner} · ${w.job.prompt_id.slice(0, 8)}${frac != null ? ` · ${Math.round(frac * 100)}%` : ""}`);
  } else setText(c.job, w.busy ? "busy" : w.state === "online" && w.enabled && !w.maintenance ? "idle" : "");
  const ws = w.warnings.join("\n");
  if (c.warnings.dataset.v !== ws) {
    c.warnings.dataset.v = ws;
    c.warnings.replaceChildren(...(w.warnings.length ? [h("ul", { class: "warnings" }, ...w.warnings.map((x) => h("li", {}, x)))] : []));
  }
  syncNote(tr._note, w.note);
  setText(tr._maint, w.maintenance ? "End maintenance" : "Maintenance");
  setText(tr._enable, w.enabled ? "Disable" : "Enable");
}
function renderWorkers() {
  const g = groupFilter ? groupName(Number(groupFilter)) : null;
  syncRows($("#workers tbody"), data.workers.filter((w) => !g || w.group === g), (w) => w.id, buildWorkerRow, updateWorkerRow);
}

// ---- recent ---------------------------------------------------------------------------------------------------
function buildRecentRow() {
  const names = ["finished", "status", "owner", "group", "worker", "run", "error", "note", "actions"];
  const cells = names.map((n) => h("td", { class: n === "finished" || n === "run" ? "num" : n === "actions" ? "actions" : n === "error" ? "err" : "" }));
  const tr = h("tr", {}, ...cells);
  tr._c = Object.fromEntries(cells.map((td, i) => [names[i], td]));
  const pid = () => tr._item.prompt_id;
  tr._note = noteInput((inp) => act(() => api("PUT", `/api/farm/jobs/${pid()}/note`, { note: inp.value }), "Note saved"));
  tr._c.note.append(tr._note);
  tr._c.actions.append(h("button", { title: "Queue a new job with the same workflow", onclick: () =>
    act(() => api("POST", `/api/farm/jobs/${pid()}/retry`), (r) => `Queued again as ${r.prompt_id.slice(0, 8)}`) }, "Retry"));
  return tr;
}
function updateRecentRow(tr, j) {
  const c = tr._c;
  setText(c.finished, j.finished_at ? `${dur(ago(j.finished_at))} ago` : "");
  pill(c.status, j.status);
  setText(c.owner, j.owner);
  setText(c.group, groupName(j.group_id));
  setText(c.worker, j.worker || "");
  setText(c.run, dur(j.run_seconds));
  setText(c.error, j.error || "");
  syncNote(tr._note, j.note);
}
function renderRecent() {
  const items = data.recent.filter((j) => !groupFilter || String(j.group_id) === groupFilter);
  syncRows($("#recent tbody"), items, (j) => j.key, buildRecentRow, updateRecentRow);
  $("#recent-empty").hidden = items.length > 0;
}

// ---- loop -----------------------------------------------------------------------------------------------------
function render() {
  document.body.classList.toggle("is-admin", data.me.admin);
  $("#scope-wrap").hidden = !data.me.admin;
  $("#scope").checked = scope === "all";
  setText($("#me"), data.me.admin ? `${data.me.user} (admin)` : data.me.user);
  renderGroups(); renderQueue(); renderWorkers(); renderRecent();
}
function showFreshness(err) {
  const banner = $("#stale");
  if (err) {
    // keep the last data on screen, but never let it pass for current: an unreachable router is not an idle farm
    banner.hidden = false;
    setText(banner, `Cannot reach the router (${err.message}).` +
      (lastOk ? ` Showing data from ${new Date(lastOk).toLocaleTimeString()}.` : ""));
  } else banner.hidden = true;
  setText($("#updated"), lastOk ? `updated ${new Date(lastOk).toLocaleTimeString()}` : "");
}
async function refresh() {
  clearTimeout(timer);
  try {
    const d = await api("GET", `/api/farm/overview?scope=${scope}`);
    clockSkew = d.now * 1000 - Date.now();
    data = d; lastOk = Date.now();
    render(); showFreshness(null);
  } catch (e) {
    showFreshness(e);
  }
  timer = setTimeout(refresh, document.hidden ? POLL_HIDDEN : POLL_VISIBLE);
}

$("#scope").addEventListener("change", (e) => {
  scope = e.target.checked ? "all" : "mine"; localStorage.setItem("comfyfarm.scope", scope); refresh();
});
$("#group-filter").addEventListener("change", (e) => {
  groupFilter = e.target.value; localStorage.setItem("comfyfarm.group", groupFilter); if (data) render();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
