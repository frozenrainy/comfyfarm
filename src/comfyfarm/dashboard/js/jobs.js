// The job table shared by the dashboard and the Jobs page: one shape for waiting, running and finished jobs.
import { locale } from "./i18n.js";
import { act, ago, api, button, copyText, dur, h, namedRow, noteInput, now, setBar, setPill, setText, syncNote, syncRows,
         t, toast, when } from "./core.js";
import { eta, pace, silence, weighted } from "./progress.js";

const QUIET_AFTER = 20;      // seconds without a word from the worker before the row says so instead of a stale %

const jobUrl = (pid, op) => `/api/farm/jobs/${pid}/${op}`;
const groupLabel = (j) => j.group ?? (j.group_id == null ? t("(any)") : `#${j.group_id}`);
const viewUrl = (f, extra = "") => "/api/view?" + new URLSearchParams({ filename: f.filename, subfolder: f.subfolder || "",
                                                                      type: f.type || "output" }) + extra;
const isVideo = (f) => /\.(mp4|webm|mov|mkv)$/i.test(f.filename);
const isImage = (f) => !isVideo(f) && (f.mediaType === "images" || /\.(png|jpe?g|webp|gif)$/i.test(f.filename));

// A small picture of a file: an image, a video's first frame, or its name (the gateway serves every kind).
function thumb(f, cls = "thumb") {
  return isImage(f) ? h("img", { class: cls, src: viewUrl(f, /\.gif$/i.test(f.filename) ? "" : "&preview=webp;60"), alt: f.filename, loading: "lazy" })
    : isVideo(f) ? h("video", { class: cls, src: viewUrl(f) + "#t=0.1", muted: true, preload: "metadata" })
    : h("span", { class: "filename" }, `▶ ${f.filename}`);
}

// Every file a job saved, in a dialog: the list shows the first and "+N".
async function showFiles(j) {
  let files;
  try { files = (await api("GET", jobUrl(j.prompt_id, "files"))).files; } catch (e) { return toast(e.message, true); }
  const dlg = h("dialog", { class: "form wide" },
    h("h3", {}, t("{n} file(s)", { n: files.length }), " ", h("span", { class: "muted small" }, j.title || j.hint || `#${j.prompt_id.slice(0, 8)}`)),
    h("div", { class: "files" }, ...files.map((f) =>
      h("a", { class: "file", href: viewUrl(f), target: "_blank", rel: "noopener", title: `${f.subfolder ? f.subfolder + "/" : ""}${f.filename}` },
        thumb(f, "thumb big"), h("div", { class: "muted small name" }, f.filename)))),
    h("div", { class: "dialog-actions" }, button(t("Close"), () => dlg.close())));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

// The whole error message, with the job id to quote when asking for help.
function showError(j) {
  const dlg = h("dialog", { class: "form wide" },
    h("h3", {}, t("Error"), " ", h("span", { class: "muted small" }, j.title || j.hint || "")),
    h("pre", { class: "code wrap" }, j.error),
    h("p", { class: "muted small" }, t("job {id}", { id: j.prompt_id }), j.worker ? ` · ${j.worker}` : ""),
    h("div", { class: "dialog-actions" }, button(t("Copy"), () => copyText(`${j.error}\n(job ${j.prompt_id})`)),
                                        button(t("Close"), () => dlg.close())));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

// The first file a job saved, and the way to the rest.
function showResult(td, j) {
  const f = j.preview;
  const key = f ? `${f.type}/${f.subfolder}/${f.filename}/${j.outputs}` : String(j.outputs ?? "");
  if (td._key === key) return;
  td._key = key;
  td.replaceChildren();
  if (!f) { if (j.outputs) td.append(button(t("{n} file(s)", { n: j.outputs }), () => showFiles(j), { cls: "link" })); return; }
  td.append(h("a", { href: viewUrl(f), target: "_blank", rel: "noopener", title: f.filename }, thumb(f)));
  if (j.outputs > 1) td.append(" ", button(`+${j.outputs - 1}`, () => showFiles(j), { cls: "link", title: t("All {n} files", { n: j.outputs }) }));
}

// What a person knows a job by: the title of its run (typed next to Run, else its workflow's tab name) and what the
// workflow says about itself (where it saves, the start of its prompt); the id when there is neither.
function showTitle(td, j) {
  const key = `${j.title}|${j.hint}`;
  if (td._key === key) return;
  td._key = key;
  td.replaceChildren(h("div", { class: "jt" }, j.title || j.hint || `#${j.prompt_id.slice(0, 8)}`),
                     j.title && j.hint ? h("div", { class: "muted small jt" }, j.hint) : "");
  td.title = [j.title, j.hint, j.prompt_id].filter(Boolean).join("\n");
}

const QUEUED = ["pending", "paused"], ACTIVE = ["pending", "paused", "dispatching", "running", "cancelling"];

// A finished time that fits a narrow column: "12m ago" today, the day and time this year, the full date before.
function shortWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso), a = ago(iso);
  if (a != null && a < 86400) return when(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString(locale() ?? [], { ...(sameYear ? {} : { year: "2-digit" }), month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

// The bar weighs ComfyUI's per-node progress by the job's plan (sampler steps), and the line under it says what the
// number cannot: the time left at the sampler's pace, that nothing has started, or that the worker has gone quiet.
function showProgress(tr, j) {
  const w = weighted(j.progress, j.plan), at = j.progress?.at;
  if (w.current && typeof at === "number") {
    const last = tr.samples[tr.samples.length - 1];
    if (!last || last.at !== at) { tr.samples.push({ at, ...w.current }); if (tr.samples.length > 8) tr.samples.shift(); }
  }
  const quiet = silence(j.progress, now() / 1000), left = eta(w, pace(tr.samples));
  setBar(tr.bar, w.frac);
  setText(tr.run, "");
  setText(tr.eta, quiet != null && quiet > QUIET_AFTER ? t("no word for {t}", { t: dur(quiet) })
    : !w.started ? t("preparing…") : left != null ? t("about {t} left", { t: dur(left) }) : "");
}

// opts: { owner: show the owner column, reorder: admin reorder buttons, refresh, list: () => the jobs shown (for reordering) }
// Columns are fixed in width (table-layout: fixed): a long title or error never pushes the others into a second line.
export function jobsTable(opts) {
  const cols = ["place", "status", "job", "result", ...(opts.owner ? ["owner"] : []), "where", "progress", "time", "note", "actions"];
  const head = { place: "#", status: t("Status"), job: t("Job"), result: t("Result"), owner: t("Owner"), where: t("Group / worker"),
                 progress: t("Progress / run time"), time: t("Time"), note: t("Note"), actions: "" };
  const cls = { place: "num", job: "job", result: "result", where: "clip", owner: "clip", progress: "progress", time: "num time clip",
                actions: "actions" };
  const width = { place: "2.4rem", status: "7.5rem", result: "8.5rem", owner: "6.5rem", where: "8rem", progress: "9.5rem",
                  time: "8.5rem", note: "8rem", actions: opts.reorder ? "16rem" : "9.5rem" };     // job: the rest
  const tbody = h("tbody");
  // the job column takes what is left — at least 16rem: on a narrow window the table scrolls sideways instead
  const fixed = cols.reduce((a, c) => a + (parseFloat(width[c]) || 0), 0);
  const el = h("div", { class: "scroll" },
    h("table", { class: "jobs", style: `min-width:${fixed + 16}rem` },
      h("colgroup", {}, ...cols.map((c) => h("col", { style: width[c] ? `width:${width[c]}` : "" }))),
      h("thead", {}, h("tr", {}, ...cols.map((c) => h("th", { class: cls[c] || "" }, head[c])))), tbody));

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
    tr.pill = h("span");
    tr.err = button("⚠", () => showError(tr._item), { cls: "err", title: t("Show the error") });
    tr.c.status.append(tr.pill, tr.err);
    tr.bar = h("span"); tr.run = h("span"); tr.eta = h("div", { class: "muted small" });
    tr.c.progress.append(tr.bar, tr.run, tr.eta);
    tr.samples = [];                                       // the current node's last reports, for its pace
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
      retry: button(t("Retry"), () => act(() => api("POST", jobUrl(pid(), "retry")),
        (r) => t("Queued again as {id}", { id: r.prompt_id.slice(0, 8) }), opts.refresh), { title: t("Queue a new job with the same workflow") }),
    };
    tr.c.actions.append(h("span", { class: "segs" }, tr.btn.top, tr.btn.up, tr.btn.down, tr.btn.bottom), " ",
                        tr.btn.pause, tr.btn.resume, tr.btn.cancel, tr.btn.retry);
    for (const k of ["top", "up", "down", "bottom"]) tr.btn[k].classList.add("seg");
    return tr;
  }
  function update(tr, j) {
    const c = tr.c, queued = QUEUED.includes(j.status), active = ACTIVE.includes(j.status);
    setText(c.place, j.place ?? "");
    if (queued && j.runnable === false) setPill(tr.pill, "blocked", `${t(j.status)} · ${t("blocked")}`);
    else setPill(tr.pill, j.status);
    tr.pill.title = queued && j.runnable === false ? t("No worker in a group you may use can run this job (requirements or permissions).") : "";
    tr.err.hidden = !j.error;
    showTitle(c.job, j);
    showResult(c.result, j);
    if (c.owner) { setText(c.owner, j.owner); c.owner.title = j.owner || ""; }
    const where = j.worker ? `${groupLabel(j)} · ${j.worker}` : groupLabel(j);
    setText(c.where, where); c.where.title = where;
    if (j.status === "running") showProgress(tr, j);
    else { setBar(tr.bar, null); setText(tr.eta, ""); setText(tr.run, active ? "" : dur(j.run_seconds)); tr.samples = []; }
    if (queued) { setText(c.time, t("waiting {t}", { t: dur(ago(j.created_at)) })); c.time.title = new Date(j.created_at).toLocaleString(locale()); }
    else if (active) { setText(c.time, dur(ago(j.started_at))); c.time.title = j.started_at ? new Date(j.started_at).toLocaleString(locale()) : ""; }
    else { setText(c.time, shortWhen(j.finished_at)); c.time.title = j.finished_at ? new Date(j.finished_at).toLocaleString(locale()) : ""; }
    syncNote(tr.note, j.note);
    const show = (k, on) => { tr.btn[k].hidden = !on; };
    for (const k of ["top", "up", "down", "bottom"]) show(k, opts.reorder && queued);
    show("pause", j.status === "pending"); show("resume", j.status === "paused");
    show("cancel", active && j.status !== "cancelling");
    show("retry", !active);
  }
  return { el, update: (items) => syncRows(tbody, items, (j) => j.key, build, update) };
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
