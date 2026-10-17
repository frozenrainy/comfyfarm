// Dashboard: only what the signed-in user needs — their jobs and place in line, the groups they may use, their own
// usage, their last 10 jobs.
import { TZ, api, dailyChart, h, qs, statCards } from "../core.js";
import { groupCards, historyTable, queueTable } from "../jobs.js";

export default {
  poll: 2000,
  mount(root, { refresh }) {
    let data = null, days = Number(localStorage.getItem("comfyfarm.dash.days")) || 7;
    const mine = queueTable({ owner: false, reorder: false, refresh, list: () => data?.queue ?? [] });
    const recent = historyTable({ owner: false, refresh });
    const groups = h("div", { class: "cards" });
    const empty = h("p", { class: "empty muted" }, "Nothing queued. Submit from ComfyUI.");
    const recentEmpty = h("p", { class: "empty muted" }, "No finished jobs yet.");
    const usage = h("div");
    const segs = [[1, "24 h"], [7, "7 days"], [30, "30 days"]].map(([d, label]) =>
      h("button", { type: "button", class: "seg", onclick: () => { days = d; localStorage.setItem("comfyfarm.dash.days", d); refresh(); } }, label));
    root.append(
      h("section", {}, h("h2", {}, "My jobs"), mine.el, empty),
      h("section", {}, h("h2", {}, "My groups"), groups),
      h("section", {}, h("div", { class: "section-head" }, h("h2", {}, "My usage"), h("div", { class: "segs" }, ...segs)), usage),
      h("section", {}, h("div", { class: "section-head" }, h("h2", {}, "Recent"), h("a", { href: "#/history" }, "All history →")),
        recent.el, recentEmpty));
    let usageSig = "";
    return {
      async refresh() {
        data = await api("GET", `/api/farm/dashboard?${qs({ days, tz: TZ })}`);
        mine.update(data.queue); empty.hidden = data.queue.length > 0; mine.el.hidden = !data.queue.length;
        groups.replaceChildren(...groupCards(data.groups));
        recent.update(data.recent); recentEmpty.hidden = data.recent.length > 0; recent.el.hidden = !data.recent.length;
        segs.forEach((b, i) => b.classList.toggle("on", [1, 7, 30][i] === days));
        const s = data.stats, sig = JSON.stringify([s.total, s.daily]);
        if (sig !== usageSig) {                    // stats change rarely; do not rebuild the chart every poll
          usageSig = sig;
          usage.replaceChildren(statCards(s.total), dailyChart(s.daily));
        }
      },
    };
  },
};
