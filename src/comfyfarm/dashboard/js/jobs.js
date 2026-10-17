// Job tables shared by the dashboard, the queue page and the history page.
import { act, ago, api, button, dur, h, namedRow, noteInput, progressOf, setBar, setPill, setText, syncNote, syncRows,
         table, when } from "./core.js";

const jobUrl = (pid, op) => `/api/farm/jobs/${pid}/${op}`;
const groupLabel = (j) => j.group ?? (j.group_id == null ? "(any)" : `#${j.group_id}`);

// opts: { owner: show owner column, reorder: admin reorder buttons, refresh, list: () => current items }
export function queueTable(opts) {
  const cols = ["place", "status", ...(opts.owner ? ["owner"] : []), "group", "worker", "progress", "time", "note", "actions"];
  const head = { place: "#", status: "Status", owner: "Owner", group: "Group", worker: "Worker", progress: "Progress",
                 time: "Waiting / running", note: "Note", actions: "" };
  const cls = { place: "num", time: "num", actions: "actions" };
  const t = table(cols.map((c) => [head[c], cls[c]]));

  function move(tr, dir) {
    const queued = opts.list().filter((j) => j.place != null);
    const i = queued.findIndex((j) => j.prompt_id === tr._item.prompt_id);
    const other = queued[i + dir];
    if (!other) return;
    act(() => api("POST", jobUrl(tr._item.prompt_id, "move"), dir < 0 ? { before: other.prompt_id } : { after: other.prompt_id }),
        null, opts.refresh);
  }
  function build() {
    const tr = namedRow(cols, cls);
    const pid = () => tr._item.prompt_id;
    tr.note = noteInput((v) => act(() => api("PUT", jobUrl(pid(), "note"), { note: v }), "Note saved", opts.refresh));
    tr.c.note.append(tr.note);
    const post = (op, body) => () => act(() => api("POST", jobUrl(pid(), op), body), null, opts.refresh);
    tr.btn = {
      top: button("⤒", post("move", { to: "top" }), { title: "Run next" }),
      up: button("↑", () => move(tr, -1), { title: "Move up" }),
      down: button("↓", () => move(tr, +1), { title: "Move down" }),
      bottom: button("⤓", post("move", { to: "bottom" }), { title: "Move to the end" }),
      pause: button("Pause", post("pause"), { title: "Keep it in the queue but skip it" }),
      resume: button("Resume", post("resume")),
      cancel: button("Cancel", () => {
        if (["dispatching", "running"].includes(tr._item.status) && !confirm("Interrupt this running job?")) return;
        post("cancel")();
      }, { cls: "danger" }),
    };
    tr.c.actions.append(...Object.values(tr.btn));
    return tr;
  }
  function update(tr, j) {
    const c = tr.c, queued = j.place != null;
    setText(c.place, queued ? j.place : "");
    if (queued && j.runnable === false) setPill(c.status, "blocked", `${j.status} · blocked`);
    else setPill(c.status, j.status);
    c.status.title = queued && j.runnable === false ? "No worker in a group you may use can run this job (requirements or permissions)." : "";
    if (c.owner) setText(c.owner, j.owner);
    setText(c.group, groupLabel(j));
    setText(c.worker, j.worker || "");
    setBar(c.progress, j.status === "running" ? progressOf(j.progress) : null);
    setText(c.time, dur(ago(queued ? j.created_at : j.started_at)));
    syncNote(tr.note, j.note);
    const show = (k, on) => { tr.btn[k].hidden = !on; };
    for (const k of ["top", "up", "down", "bottom"]) show(k, opts.reorder && queued);
    show("pause", j.status === "pending"); show("resume", j.status === "paused");
    show("cancel", j.status !== "cancelling");
  }
  return { el: t.el, update: (items) => syncRows(t.tbody, items, (j) => j.key, build, update) };
}

export function historyTable(opts) {
  const cols = ["finished", "status", ...(opts.owner ? ["owner"] : []), "group", "worker", "run", "error", "note", "actions"];
  const head = { finished: "Finished", status: "Status", owner: "Owner", group: "Group", worker: "Worker", run: "Run time",
                 error: "Error", note: "Note", actions: "" };
  const cls = { finished: "num", run: "num", error: "err", actions: "actions" };
  const t = table(cols.map((c) => [head[c], cls[c]]));
  function build() {
    const tr = namedRow(cols, cls);
    const pid = () => tr._item.prompt_id;
    tr.note = noteInput((v) => act(() => api("PUT", jobUrl(pid(), "note"), { note: v }), "Note saved", opts.refresh));
    tr.c.note.append(tr.note);
    tr.c.actions.append(button("Retry", () => act(() => api("POST", jobUrl(pid(), "retry")),
      (r) => `Queued again as ${r.prompt_id.slice(0, 8)}`, opts.refresh), { title: "Queue a new job with the same workflow" }));
    return tr;
  }
  function update(tr, j) {
    const c = tr.c;
    setText(c.finished, when(j.finished_at));
    c.finished.title = j.finished_at ? new Date(j.finished_at).toLocaleString() : "";
    setPill(c.status, j.status);
    if (c.owner) setText(c.owner, j.owner);
    setText(c.group, groupLabel(j));
    setText(c.worker, j.worker || "");
    setText(c.run, dur(j.run_seconds));
    setText(c.error, j.error || "");
    c.error.title = `job ${j.prompt_id}`;
    syncNote(tr.note, j.note);
  }
  return { el: t.el, update: (items) => syncRows(t.tbody, items, (j) => j.key, build, update) };
}

// Group cards: health numbers of the groups the user may use.
export function groupCards(groups) {
  if (!groups.length) return [h("p", { class: "muted" }, "You may not use any worker group yet. Ask an administrator.")];
  return groups.map((g) => {
    const hh = g.health || {};
    const stall = hh.dispatch_idle_seconds;
    const stallCls = stall == null ? "" : stall >= 300 ? "alert" : stall >= 60 ? "caution" : "";
    const row = (k, v, c = "") => [h("dt", {}, k), h("dd", { class: c }, v)];
    return h("div", { class: "card" },
      h("h3", {}, h("span", {}, g.name), h("span", { class: "muted" }, `${g.online}/${g.workers} online`)),
      h("dl", {},
        row("Idle workers", String(hh.idle_workers ?? "—")),
        row("Waiting", String(hh.pending ?? "—")),
        row("Running", String(hh.running ?? "—")),
        row("Paused", String(hh.paused ?? "—")),
        row("Dispatch stalled", stall == null ? "—" : dur(stall), stallCls),
        row("Longest running", dur(hh.running_longest_seconds)),
        hh.unsatisfiable_pending ? row("Cannot run", `${hh.unsatisfiable_pending} job(s)`, "alert") : null,
        hh.inconsistent_workers ? row("Consistency", `${hh.inconsistent_workers} worker(s) differ`, "caution") : null));
  });
}
