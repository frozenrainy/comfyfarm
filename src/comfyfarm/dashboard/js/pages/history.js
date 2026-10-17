// History: every finished job, searchable and paged. Not polled — a list that moves while one reads it is useless.
import { TZ, api, button, h, qs, setText, state } from "../core.js";
import { historyTable } from "../jobs.js";
import { groupSelect, searchBox } from "./queue.js";

const PAGE = 50;
const STATUS = [["", "all"], ["completed", "completed"], ["failed", "failed"], ["cancelled", "cancelled"]];

export default {
  poll: 0,
  mount(root, { refresh }) {
    const admin = state.me.admin;
    const f = { scope: admin && localStorage.getItem("comfyfarm.scope") === "all" ? "all" : "mine", q: "", status: "",
                group: "", start: "", end: "", offset: 0 };
    const set = (k) => (v) => { f[k] = v; f.offset = 0; refresh(); };
    const groupSel = groupSelect(set("group"));
    groupSel.load();
    const from = h("input", { type: "date", onchange: (e) => set("start")(e.target.value) });
    const to = h("input", { type: "date", onchange: (e) => set("end")(e.target.value) });
    const scope = h("label", { class: "check", hidden: !admin },
      h("input", { type: "checkbox", checked: f.scope === "all", onchange: (e) => {
        f.scope = e.target.checked ? "all" : "mine"; localStorage.setItem("comfyfarm.scope", f.scope); f.offset = 0; rebuild(); refresh();
      } }), " all users");
    const pageInfo = h("span", { class: "muted" });
    const prev = button("← Newer", () => { f.offset = Math.max(0, f.offset - PAGE); refresh(); });
    const next = button("Older →", () => { f.offset += PAGE; refresh(); });
    const holder = h("div"), empty = h("p", { class: "empty muted" }, "No matching jobs.");
    let tbl;
    function rebuild() { tbl = historyTable({ owner: f.scope === "all", refresh }); holder.replaceChildren(tbl.el); }
    rebuild();
    root.append(h("section", {},
      h("div", { class: "section-head" }, h("h2", {}, "History"), button("Refresh", () => refresh())),
      h("div", { class: "filters" },
        searchBox(admin ? "search id, note, error, owner" : "search id, note or error", set("q")),
        h("select", { onchange: (e) => set("status")(e.target.value) }, ...STATUS.map(([v, l]) => h("option", { value: v }, l))),
        groupSel.el, h("span", { class: "dates" }, from, " – ", to), scope),
      holder, empty, h("div", { class: "pager" }, prev, pageInfo, next)));
    return {
      async refresh() {
        const q = { ...f, limit: PAGE, tz: TZ };
        if (f.start || f.end) {                    // one open end: from the first day / up to today
          q.start = f.start || "2000-01-01";
          q.end = f.end || new Date().toLocaleDateString("sv");
        }
        const d = await api("GET", `/api/farm/history?${qs(q)}`);
        tbl.update(d.jobs);
        empty.hidden = d.jobs.length > 0;
        tbl.el.hidden = !d.jobs.length;
        setText(pageInfo, d.total ? `${d.offset + 1}–${d.offset + d.jobs.length} of ${d.total}` : "");
        prev.disabled = d.offset === 0;
        next.disabled = d.offset + d.jobs.length >= d.total;
      },
    };
  },
};
