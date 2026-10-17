// ComfyFarm web UI shell: hash routes → page modules (docs/design/13-web-ui.md). Each page mounts into <main> and
// exposes refresh(); pages showing live state poll, the others refresh on demand.
import { $, api, applyTheme, setText, state } from "./js/core.js";
import dashboard from "./js/pages/dashboard.js";
import queue from "./js/pages/queue.js";
import history from "./js/pages/history.js";
import settings from "./js/pages/settings.js";
import admin from "./js/pages/admin.js";

const PAGES = { "": dashboard, queue, history, settings, admin };
const POLL_HIDDEN = 15000;
let current = null, timer = null, lastOk = null, generation = 0;

async function loadMe() {
  state.me = await api("GET", "/api/farm/me");
  applyTheme(state.me.prefs.theme);
  document.body.classList.toggle("is-admin", state.me.admin);
  setText($("#me"), state.me.admin ? `${state.me.user} (admin)` : state.me.user);
}

function showFreshness(err) {
  const banner = $("#stale");
  if (err && err.status !== 401) {
    // keep the last data on screen, but never let it pass for current: an unreachable router is not an idle farm
    banner.hidden = false;
    setText(banner, `Cannot load data (${err.message}).` + (lastOk ? ` Showing data from ${new Date(lastOk).toLocaleTimeString()}.` : ""));
  } else if (!err) banner.hidden = true;
  setText($("#updated"), lastOk ? `updated ${new Date(lastOk).toLocaleTimeString()}` : "");
}

async function tick() {
  clearTimeout(timer);
  const gen = generation, page = current;
  if (!page) return;
  try {
    await page.refresh();
    if (gen !== generation) return;
    lastOk = Date.now(); showFreshness(null);
  } catch (e) {
    if (gen !== generation) return;
    showFreshness(e);
  }
  if (page.poll) timer = setTimeout(tick, document.hidden ? POLL_HIDDEN : page.poll);
}

function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [name, sub] = path.split("/");
  const page = PAGES[name] ?? dashboard;
  generation++;
  clearTimeout(timer);
  current?.unmount?.();
  const root = $("#page");
  root.replaceChildren();
  for (const a of document.querySelectorAll("header [data-page]")) a.classList.toggle("on", a.dataset.page === (PAGES[name] ? name : ""));
  if (page.adminOnly && !state.me.admin) {
    root.textContent = "Administrators only.";
    current = null;
    return;
  }
  current = page.mount(root, { sub, params: new URLSearchParams(query || ""), refresh: tick, reloadMe: loadMe });
  current.poll ??= page.poll;
  tick();
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden && current?.poll) tick(); });
loadMe().then(route, (e) => showFreshness(e));
