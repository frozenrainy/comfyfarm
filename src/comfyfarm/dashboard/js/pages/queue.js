// Queue: unfinished jobs with search and filters. Admins see everybody's jobs and may reorder them (D-32).
import { api, h, qs, setText, state, t } from "../core.js";
import { queueTable } from "../jobs.js";

const STATUS = [["", "all unfinished"], ["pending", "waiting"], ["paused", "paused"],
                ["dispatching,running,cancelling", "running"]];         // [value, English label]

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
  const inp = h("input", { type: "search", class: "search", placeholder, oninput: () => { clearTimeout(timer); timer = setTimeout(() => onchange(inp.value), 300); } });
  return inp;
}

export default {
  poll: 2000,
  mount(root, { refresh }) {
    const admin = state.me.admin;
    const f = { scope: admin && localStorage.getItem("comfyfarm.scope") === "all" ? "all" : "mine", q: "", status: "", group: "" };
    let data = null;
    const set = (k) => (v) => { f[k] = v; refresh(); };
    const groupSel = groupSelect(set("group"));
    groupSel.load();
    const scope = h("label", { class: "check", hidden: !admin },
      h("input", { type: "checkbox", checked: f.scope === "all", onchange: (e) => {
        f.scope = e.target.checked ? "all" : "mine"; localStorage.setItem("comfyfarm.scope", f.scope); rebuild(); refresh();
      } }), " " + t("all users"));
    const summary = h("span", { class: "muted" });
    const tableHolder = h("div");
    const empty = h("p", { class: "empty muted" }, t("No matching jobs."));
    let tbl;
    function rebuild() {
      tbl = queueTable({ owner: f.scope === "all", reorder: admin, refresh, list: () => data?.jobs ?? [] });
      tableHolder.replaceChildren(tbl.el);
    }
    rebuild();
    root.append(h("section", {},
      h("div", { class: "section-head" }, h("h2", {}, t("Queue")), summary),
      h("div", { class: "filters" },
        searchBox(admin ? t("search title, id, note, owner") : t("search title, id or note"), set("q")),
        h("select", { onchange: (e) => set("status")(e.target.value) }, ...STATUS.map(([v, l]) => h("option", { value: v }, t(l)))),
        groupSel.el, scope),
      tableHolder, empty,
      admin ? h("p", { class: "muted small" }, t("⤒ run next · ↑↓ move past the neighbour shown · ⤓ to the end. One order is shared by all groups.")) : null));
    return {
      async refresh() {
        data = await api("GET", `/api/farm/queue?${qs(f)}`);
        tbl.update(data.jobs);
        empty.hidden = data.jobs.length > 0;
        tbl.el.hidden = !data.jobs.length;
        setText(summary, t("farm: {queued} waiting · {running} running", data.farm));
      },
    };
  },
};
