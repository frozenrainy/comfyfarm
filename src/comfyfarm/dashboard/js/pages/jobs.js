// Jobs: the queue and the history in one table — unfinished jobs on top, live; finished ones below, newest first,
// 50 a page. Admins see everybody's jobs and may reorder the waiting ones (D-32).
import { TZ, api, button, h, qs, setText, state, t } from "../core.js";
import { jobsTable } from "../jobs.js";

const PAGE = 50, FINISHED_EVERY = 30000;
const ACTIVE = ["pending", "paused", "dispatching", "running", "cancelling"], DONE = ["completed", "failed", "cancelled"];
const STATUS = [["", "all jobs"], ["pending", "waiting"], ["paused", "paused"], ["dispatching,running,cancelling", "running"],
                [DONE.join(","), "all finished"], ["completed", "completed"], ["failed", "failed"], ["cancelled", "cancelled"]];

export function groupSelect(onchange, allLabel = t("all groups")) {
  const sel = h("select", { onchange: () => onchange(sel.value) }, h("option", { value: "" }, allLabel));
  return {
    el: sel,
    async load() {
      const groups = state.me.admin ? await api("GET", "/api/admin/groups") : state.me.groups;
      sel.append(...groups.map((g) => h("option", { value: String(g.id) }, g.name)));
    },
  };
}

export function searchBox(placeholder, onchange) {
  let timer;
  return h("input", { type: "search", class: "search", placeholder,
                      oninput(e) { clearTimeout(timer); timer = setTimeout(() => onchange(e.target.value), 300); } });
}

export default {
  poll: 2000,
  mount(root, { params, refresh }) {
    const admin = state.me.admin;
    const wanted = params.get("status") === "finished" ? DONE.join(",") : params.get("status") || "";
    const f = { scope: admin && localStorage.getItem("comfyfarm.scope") === "all" ? "all" : "mine", q: "", group: "",
                status: STATUS.some(([v]) => v === wanted) ? wanted : "", start: "", end: "", offset: 0 };
    let active = [], done = { jobs: [], total: 0, offset: 0 }, activeSig = "", doneAt = 0, doneKey = "";
    const set = (k) => (v) => { f[k] = v; f.offset = 0; refresh(); };
    const groupSel = groupSelect(set("group"));
    groupSel.load();
    const statusSel = h("select", { onchange: (e) => set("status")(e.target.value) },
      ...STATUS.map(([v, l]) => h("option", { value: v, selected: v === f.status }, t(l))));
    const from = h("input", { type: "date", title: t("Finished from"), onchange: (e) => set("start")(e.target.value) });
    const to = h("input", { type: "date", title: t("Finished until"), onchange: (e) => set("end")(e.target.value) });
    const scope = h("label", { class: "check", hidden: !admin },
      h("input", { type: "checkbox", checked: f.scope === "all", onchange: (e) => {
        f.scope = e.target.checked ? "all" : "mine"; localStorage.setItem("comfyfarm.scope", f.scope); f.offset = 0; rebuild(); refresh();
      } }), " " + t("all users"));
    const summary = h("span", { class: "muted" });
    const holder = h("div"), empty = h("p", { class: "empty muted" }, t("No matching jobs."));
    const pageInfo = h("span", { class: "muted" });
    const prev = button(t("← Newer"), () => { f.offset = Math.max(0, f.offset - PAGE); refresh(); });
    const next = button(t("Older →"), () => { f.offset += PAGE; refresh(); });
    const pager = h("div", { class: "pager" }, prev, pageInfo, next);
    let tbl;
    function rebuild() {
      tbl = jobsTable({ owner: f.scope === "all", reorder: admin, refresh, list: () => active });
      holder.replaceChildren(tbl.el);
    }
    rebuild();
    root.append(h("section", {},
      h("div", { class: "section-head" }, h("h2", {}, t("Jobs")), summary, h("span", { class: "spacer" }), button(t("Refresh"), () => { doneAt = 0; refresh(); })),
      h("div", { class: "filters" },
        searchBox(admin ? t("search title, id, note, error, owner") : t("search title, id, note or error"), set("q")),
        statusSel, groupSel.el, h("span", { class: "dates" }, from, " – ", to), scope),
      holder, empty, pager,
      admin ? h("p", { class: "muted small" }, t("⤒ run next · ↑↓ move past the neighbour shown · ⤓ to the end. One order is shared by all groups.")) : null));

    return {
      async refresh() {
        const sel = f.status ? f.status.split(",") : null;
        const activeWanted = sel ? sel.filter((s) => ACTIVE.includes(s)) : ACTIVE;
        const doneWanted = sel ? sel.filter((s) => DONE.includes(s)) : DONE;
        const common = { scope: f.scope, q: f.q, group: f.group };
        let farm = null;
        if (activeWanted.length) {
          const d = await api("GET", `/api/farm/queue?${qs({ ...common, status: activeWanted.join(",") })}`);
          active = d.jobs; farm = d.farm;
        } else active = [];
        // the finished list moves only when a job finishes: read it again then, on a filter or page change, or every 30 s
        const sig = active.map((j) => `${j.key}:${j.status}`).join(",");
        const key = JSON.stringify([common, doneWanted, f.start, f.end, f.offset]);
        if (doneWanted.length) {
          if (sig !== activeSig || key !== doneKey || Date.now() - doneAt > FINISHED_EVERY) {
            const q = { ...common, status: doneWanted.join(","), offset: f.offset, limit: PAGE, tz: TZ };
            if (f.start || f.end) { q.start = f.start || "2000-01-01"; q.end = f.end || new Date().toLocaleDateString("sv"); }
            done = await api("GET", `/api/farm/history?${qs(q)}`);
            doneAt = Date.now(); doneKey = key;
          }
        } else done = { jobs: [], total: 0, offset: 0 };
        activeSig = sig;
        const rows = [...active, ...done.jobs];
        tbl.update(rows);
        empty.hidden = rows.length > 0;
        tbl.el.hidden = !rows.length;
        setText(summary, farm ? t("farm: {queued} waiting · {running} running", farm) : "");
        pager.hidden = !doneWanted.length || done.total <= PAGE;
        setText(pageInfo, done.total ? t("{from}–{to} of {total}", { from: done.offset + 1, to: done.offset + done.jobs.length, total: done.total }) : "");
        prev.disabled = done.offset === 0;
        next.disabled = done.offset + done.jobs.length >= done.total;
      },
    };
  },
};
