// ComfyFarm web UI shell: hash routes → page modules (docs/design/13-web-ui.md). Each page mounts into <main> and
// exposes refresh(); pages showing live state poll, the others refresh on demand.
import { $, api, applyTheme, setText, state } from "./js/core.js";
import dashboard from "./js/pages/dashboard.js";
import queue from "./js/pages/queue.js";
import history from "./js/pages/history.js";
import settings from "./js/pages/settings.js";
import admin from "./js/pages/admin.js";

const PAGES = { "": dashboard, queue, history, settings, admin };
const POLL_HIDDEN = 15000, BADGE_EVERY = 5000;
let current = null, timer = null, lastOk = null, generation = 0, frame = null;

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

// ---- Workflow tab: stock ComfyUI (same origin, through the gateway) under the ComfyFarm header -------------------
function showWorkflow(on) {
  document.body.classList.toggle("workflow", on);
  $("#workflow").hidden = !on;
  if (on && !frame) {                       // created once: switching tabs hides it, never reloads it
    frame = document.createElement("iframe");
    frame.title = "ComfyUI";
    frame.src = "/";
    frame.allow = "clipboard-read; clipboard-write; fullscreen";
    $("#workflow").append(frame);
  }
}

async function updateBadge() {
  try {
    const s = await api("GET", "/api/farm/summary");
    const b = $("#qbadge"), n = s.queued + s.running;
    b.hidden = n === 0;
    setText(b, String(n));
    b.classList.toggle("running", s.running > 0);
    b.title = `${s.running} running · ${s.queued} waiting`;
  } catch { /* the page's own refresh reports outages */ }
}

// Messages from the ComfyUI extension in the Workflow tab — only from that frame, only from this origin.
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !frame || e.source !== frame.contentWindow) return;
  if (e.data?.type === "comfyfarm:queued") {
    updateBadge();
    if (current?.poll) tick();
  }
});

function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [name, sub] = path.split("/");
  generation++;
  clearTimeout(timer);
  current?.unmount?.();
  for (const a of document.querySelectorAll("header [data-page]")) a.classList.toggle("on", a.dataset.page === (name in PAGES || name === "comfyui" ? name : ""));
  showWorkflow(name === "comfyui");
  if (name === "comfyui") {
    current = null;
    $("#stale").hidden = true;
    return;
  }
  const page = PAGES[name] ?? dashboard;
  const root = $("#page");
  root.replaceChildren();
  if (page.adminOnly && !state.me.admin) {
    root.textContent = "Administrators only.";
    current = null;
    return;
  }
  current = page.mount(root, { sub, params: new URLSearchParams(query || ""), refresh: tick, reloadMe: loadMe });
  current.poll ??= page.poll;
  tick();
}

window.addEventListener("hashchange", () => { if (state.me) route(); });   // before loadMe: its .then(route) routes
document.addEventListener("visibilitychange", () => { if (!document.hidden && current?.poll) tick(); });
loadMe().then(route, (e) => showFreshness(e));
updateBadge();
setInterval(updateBadge, BADGE_EVERY);
