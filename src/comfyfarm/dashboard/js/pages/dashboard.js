// Dashboard: only what the signed-in user needs — their jobs and place in line, the groups they may use, their own
// usage, their last 10 jobs.
import { TZ, api, button, dailyChart, h, pill, qs, statCards, t } from "../core.js";
import { groupCards, historyTable, queueTable } from "../jobs.js";

export default {
  poll: 2000,
  mount(root, { refresh }) {
    let data = null, days = Number(localStorage.getItem("comfyfarm.dash.days")) || 7;
    const mine = queueTable({ owner: false, reorder: false, refresh, list: () => data?.queue ?? [] });
    const recent = historyTable({ owner: false, refresh });
    const groups = h("div", { class: "cards" });
    const empty = h("p", { class: "empty muted" }, t("Nothing queued. Submit from ComfyUI."));
    const recentEmpty = h("p", { class: "empty muted" }, t("No finished jobs yet."));
    const usage = h("div");
    // Administrators of a new farm: the first day, step by step, until it is done (or hidden)
    const start = h("section", { hidden: true });
    const STEPS = {
      ui: [t("The UI worker is ready"), t("ComfyUI opens in the browser. It builds and starts by itself on the first start — a minute or two."), "#/admin/rollout"],
      build: [t("A build for your GPU, and its profiles"), t("System → Rollout: add node packs if you want them, Build for your kind of GPU, then From a recipe."), "#/admin/rollout"],
      gpu: [t("A GPU worker running"), t("System → Workers → Set up a worker: the GPU machine (this one, too). It makes the first group."), "#/admin/workers"],
      user: [t("An artist account"), t("System → Users: an account with the group; artists sign in at this address."), "#/admin/users"],
      job: [t("A first job"), t("Open ComfyUI, pick the group, queue a workflow."), "/"],
    };
    let startSig = "";
    async function gettingStarted() {
      if (!document.body.classList.contains("is-admin") || localStorage.getItem("comfyfarm.start.hidden") === "1") return;
      let g;
      try { g = await api("GET", "/api/admin/getting-started"); } catch { return; }
      const sig = JSON.stringify(g);
      if (sig === startSig) return;
      startSig = sig;
      const next = g.steps.find((s) => !s.done);
      start.hidden = !next;
      if (!next) return;
      const rows = g.steps.map((s, i) => {
        const [title, how, href] = STEPS[s.key] || [s.key, "", "#/"];
        const detail = s.key === "ui" && !s.done && g.first_start?.detail ? g.first_start.detail : how;
        return h("li", { class: s.done ? "done" : s === next ? "next" : "" },
          h("span", { class: "mark" }, s.done ? "✓" : String(i + 1)), h("div", {},
            h("strong", {}, title), " ", s === next ? h("a", { href, class: "go" }, s.key === "job" ? t("Open ComfyUI →") : t("Go →")) : "",
            h("div", { class: "muted small" }, detail)));
      });
      start.replaceChildren(h("div", { class: "section-head" }, h("h2", {}, t("Getting started")),
        pill(g.steps.filter((s) => s.done).length === g.steps.length ? "done" : "running",
             t("{n} of {m}", { n: g.steps.filter((s) => s.done).length, m: g.steps.length })),
        button(t("Hide"), () => { localStorage.setItem("comfyfarm.start.hidden", "1"); start.hidden = true; })),
        h("ol", { class: "steps" }, ...rows));
    }
    const segs = [[1, t("24 h")], [7, t("7 days")], [30, t("30 days")]].map(([d, label]) =>
      h("button", { type: "button", class: "seg", onclick: () => { days = d; localStorage.setItem("comfyfarm.dash.days", d); refresh(); } }, label));
    root.append(
      start,
      h("section", {}, h("h2", {}, t("My jobs")), mine.el, empty),
      h("section", {}, h("h2", {}, t("My groups")), groups),
      h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("My usage")), h("div", { class: "segs" }, ...segs)), usage),
      h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("Recent")), h("a", { href: "#/history" }, t("All history →"))),
        recent.el, recentEmpty));
    let usageSig = "";
    return {
      async refresh() {
        data = await api("GET", `/api/farm/dashboard?${qs({ days, tz: TZ })}`);
        gettingStarted();
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
