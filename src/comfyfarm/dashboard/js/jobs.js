// Job tables shared by the dashboard, the queue page and the history page.
import { locale } from "./i18n.js";
import { act, ago, api, button, dur, h, namedRow, noteInput, progressOf, setBar, setPill, setText, syncNote, syncRows,
         t, table, when } from "./core.js";

const jobUrl = (pid, op) => `/api/farm/jobs/${pid}/${op}`;
const groupLabel = (j) => j.group ?? (j.group_id == null ? t("(any)") : `#${j.group_id}`);
const viewUrl = (f, extra = "") => "/api/view?" + new URLSearchParams({ filename: f.filename, subfolder: f.subfolder || "",
                                                                      type: f.type || "output" }) + extra;

// The first file a job saved: a small picture of an image, a link to anything else (the gateway serves both).
function showResult(td, j) {
  const f = j.preview;
  const key = f ? `${f.type}/${f.subfolder}/${f.filename}/${j.outputs}` : String(j.outputs ?? "");
  if (td._key === key) return;
  td._key = key;
  td.replaceChildren();
  if (!f) { if (j.outputs) td.append(t("{n} file(s)", { n: j.outputs })); return; }
  const image = f.mediaType === "images" && !/\.(mp4|webm|mov|gif)$/i.test(f.filename);
  td.append(h("a", { href: viewUrl(f), target: "_blank", rel: "noopener", title: f.filename },
    image ? h("img", { class: "thumb", src: viewUrl(f, "&preview=webp;60"), alt: f.filename, loading: "lazy" })
          : `▶ ${f.filename}`));
  if (j.outputs > 1) td.append(h("span", { class: "muted" }, ` +${j.outputs - 1}`));
}

// opts: { owner: show owner column, reorder: admin reorder buttons, refresh, list: () => current items }
export function queueTable(opts) {
  const cols = ["place", "status", ...(opts.owner ? ["owner"] : []), "group", "worker", "progress", "time", "note", "actions"];
  const head = { place: "#", status: t("Status"), owner: t("Owner"), group: t("Group"), worker: t("Worker"), progress: t("Progress"),
                 time: t("Waiting / running"), note: t("Note"), actions: "" };
  const cls = { place: "num", time: "num", actions: "actions" };
  const tbl = table(cols.map((c) => [head[c], cls[c]]));

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
    tr.note = noteInput((v) => act(() => api("PUT", jobUrl(pid(), "note"), { note: v }), t("Note saved"), opts.refresh));
    tr.c.note.append(tr.note);
    const post = (op, body) => () => act(() => api("POST", jobUrl(pid(), op), body), null, opts.refresh);
    tr.btn = {
      top: button("⤒", post("move", { to: "top" }), { title: t("Run next") }),
      up: button("↑", () => move(tr, -1), { title: t("Move up") }),
      down: button("↓", () => move(tr, +1), { title: t("Move down") }),
      bottom: button("⤓", post("move", { to: "bottom" }), { title: t("Move to the end") }),
      pause: button(t("Pause"), post("pause"), { title: t("Keep it in the queue but skip it") }),
      resume: button(t("Resume"), post("resume")),
      cancel: button(t("Cancel"), () => {
        if (["dispatching", "running"].includes(tr._item.status) && !confirm(t("Interrupt this running job?"))) return;
        post("cancel")();
      }, { cls: "danger" }),
    };
    tr.c.actions.append(...Object.values(tr.btn));
    return tr;
  }
  function update(tr, j) {
    const c = tr.c, queued = j.place != null;
    setText(c.place, queued ? j.place : "");
    if (queued && j.runnable === false) setPill(c.status, "blocked", `${t(j.status)} · ${t("blocked")}`);
    else setPill(c.status, j.status);
    c.status.title = queued && j.runnable === false ? t("No worker in a group you may use can run this job (requirements or permissions).") : "";
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
  return { el: tbl.el, update: (items) => syncRows(tbl.tbody, items, (j) => j.key, build, update) };
}

export function historyTable(opts) {
  const cols = ["finished", "status", "result", ...(opts.owner ? ["owner"] : []), "group", "worker", "run", "error", "note",
                "actions"];
  const head = { finished: t("Finished"), status: t("Status"), result: t("Result"), owner: t("Owner"), group: t("Group"),
                 worker: t("Worker"), run: t("Run time"), error: t("Error"), note: t("Note"), actions: "" };
  const cls = { finished: "num", result: "result", run: "num", error: "err", actions: "actions" };
  const tbl = table(cols.map((c) => [head[c], cls[c]]));
  function build() {
    const tr = namedRow(cols, cls);
    const pid = () => tr._item.prompt_id;
    tr.note = noteInput((v) => act(() => api("PUT", jobUrl(pid(), "note"), { note: v }), t("Note saved"), opts.refresh));
    tr.c.note.append(tr.note);
    tr.c.actions.append(button(t("Retry"), () => act(() => api("POST", jobUrl(pid(), "retry")),
      (r) => t("Queued again as {id}", { id: r.prompt_id.slice(0, 8) }), opts.refresh), { title: t("Queue a new job with the same workflow") }));
    return tr;
  }
  function update(tr, j) {
    const c = tr.c;
    setText(c.finished, when(j.finished_at));
    c.finished.title = j.finished_at ? new Date(j.finished_at).toLocaleString(locale()) : "";
    setPill(c.status, j.status);
    showResult(c.result, j);
    if (c.owner) setText(c.owner, j.owner);
    setText(c.group, groupLabel(j));
    setText(c.worker, j.worker || "");
    setText(c.run, dur(j.run_seconds));
    setText(c.error, j.error || "");
    c.error.title = t("job {id}", { id: j.prompt_id });
    syncNote(tr.note, j.note);
  }
  return { el: tbl.el, update: (items) => syncRows(tbl.tbody, items, (j) => j.key, build, update) };
}

// Group cards: health numbers of the groups the user may use.
export function groupCards(groups) {
  if (!groups.length) {
    // an administrator of a new farm is the one to ask: the first group comes with the first GPU worker
    return [h("p", { class: "muted" }, document.body.classList.contains("is-admin")
      ? t("No worker group yet: set up the first GPU worker (System → Workers) — it makes the first group.")
      : t("You may not use any worker group yet. Ask an administrator."))];
  }
  const admin = document.body.classList.contains("is-admin");     // operator numbers are for operators (QA, dev.11)
  return groups.map((g) => {
    const hh = g.health || {};
    const stall = hh.dispatch_idle_seconds;
    const stallCls = stall == null ? "" : stall >= 300 ? "alert" : stall >= 60 ? "caution" : "";
    const row = (k, v, c = "") => [h("dt", {}, k), h("dd", { class: c }, v)];
    return h("div", { class: "card" },
      h("h3", {}, h("span", {}, g.name), h("span", { class: "muted" }, t("{online}/{workers} online", g))),
      h("dl", {},
        row(t("Idle workers"), String(hh.idle_workers ?? "—")),
        row(t("Waiting"), String(hh.pending ?? "—")),
        row(t("Running"), String(hh.running ?? "—")),
        admin ? row(t("Paused"), String(hh.paused ?? "—")) : null,
        admin ? row(t("Dispatch stalled"), stall == null ? "—" : dur(stall), stallCls) : null,
        row(t("Longest running"), dur(hh.running_longest_seconds)),
        hh.unsatisfiable_pending ? row(t("Cannot run"), t("{n} job(s)", { n: hh.unsatisfiable_pending }), "alert") : null,
        admin && hh.inconsistent_workers ? row(t("Consistency"), t("{n} worker(s) differ", { n: hh.inconsistent_workers }), "caution") : null));
  });
}
