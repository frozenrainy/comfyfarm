// SPDX-License-Identifier: GPL-3.0-or-later
// ComfyFarm frontend extension (see ../__init__.py). Official extension API only — no frontend fork.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const GROUP_KEY = "comfyfarm.workflowGroup";                // {id, name} in localStorage; absent = the site default
const TITLES_KEY = "comfyfarm.runTitles";                  // {workflow path: title typed for its runs}
const RETRY_DELAYS_S = [1, 2, 3, 4, 5, 5, 5, 5, 5, 5];   // D-23: retry the same prompt_id, never a new one
const ATTEMPT_TIMEOUT_MS = 20000;                          // stock fetch has no timeout; a dead primary hangs minutes

let groups = [];
let submitting = false;

// A v4 UUID for each submission. crypto.randomUUID exists in secure contexts only (HTTPS, localhost): a farm opened
// at http://<address> has crypto.getRandomValues but not it (field, dev.19: the first Run failed with a TypeError).
function uuid4() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;                             // version 4
  b[8] = (b[8] & 0x3f) | 0x80;                             // RFC 4122 variant
  const x = [...b].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function storedGroup() {
  try {
    const g = JSON.parse(localStorage.getItem(GROUP_KEY) || "null");
    return g && typeof g.id === "number" && typeof g.name === "string" ? g : null;
  } catch {
    return null;
  }
}

// The dashboard's language choice (comfyfarm.lang), else the browser's — same rule as the dashboard.
const KO = {
  "Worker group for Run": "실행할 워커 그룹", "Any of my groups": "내 그룹 중 아무 곳", "{n} online": "{n}대 온라인",
  "No worker group available to you.": "사용할 수 있는 워커 그룹이 없습니다.", "Group": "그룹", "any": "아무 곳",
  "Worker group for Run — the node list follows it": "실행할 워커 그룹 — 노드 목록도 이 그룹을 따릅니다",
  "Title of these runs": "이 워크플로 실행 제목", "empty: the tab's name ({name})": "비우면 탭 이름 ({name})",
  "empty: no title": "비우면 제목 없음", "My queue": "내 큐", "My jobs: titles, state, results": "내 작업 — 제목·상태·결과",
  "Nothing queued, nothing finished yet.": "대기 중이거나 끝난 작업이 없습니다.", "Open in the dashboard": "대시보드에서 보기",
  "pending": "대기", "paused": "일시정지", "dispatching": "보내는 중", "running": "실행 중", "cancelling": "취소 중",
  "completed": "완료", "failed": "실패", "cancelled": "취소됨", "#{n} in line": "{n}번째", "Running": "실행 중",
  "Finished": "끝남",
};
function lang() {
  const pref = localStorage.getItem("comfyfarm.lang") || "auto";
  if (pref !== "auto") return pref;
  return (navigator.languages?.[0] || navigator.language || "en").toLowerCase().startsWith("ko") ? "ko" : "en";
}
const tr = (s) => (lang() === "ko" ? KO[s] ?? s : s);

// ---- the title of a run: typed for the open workflow, else its tab's name (field, dev.17: queued jobs looked alike) --
function activeTab() {
  const w = app.extensionManager?.workflow?.activeWorkflow;            // the frontend's workflow store (1.2x and later)
  const name = typeof w?.filename === "string" ? w.filename : "";
  return { key: typeof w?.path === "string" ? w.path : "", name: w?.isTemporary && /^Unsaved Workflow/i.test(name) ? "" : name };
}
function typedTitles() {
  try {
    const m = JSON.parse(localStorage.getItem(TITLES_KEY) || "{}");
    return m && typeof m === "object" ? m : {};
  } catch {
    return {};
  }
}
function setTypedTitle(key, title) {
  const m = typedTitles();
  delete m[key];
  if (title.trim()) m[key] = title.trim().slice(0, 100);
  const keys = Object.keys(m);
  for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete m[k];      // the 50 most recent tabs
  localStorage.setItem(TITLES_KEY, JSON.stringify(m));
}
function runTitle() {
  const tab = activeTab();
  return (tab.key && typedTitles()[tab.key]) || tab.name || "";
}

const origFetchApi = api.fetchApi.bind(api);

api.fetchApi = async function (route, options = {}) {
  const g = storedGroup();
  // the id, not the name: header values must be Latin-1, and a group may be called "렌더팀"
  options.headers = { ...(options.headers || {}), ...(g ? { "X-ComfyFarm-Group": String(g.id) } : {}) };
  const isSubmit = (route === "/prompt" || route === "/api/prompt") && (options.method || "GET").toUpperCase() === "POST";
  if (!isSubmit) return origFetchApi(route, options);

  if (submitting) throw new Error("ComfyFarm: a submission is still being retried");
  const body = JSON.parse(options.body);
  body.prompt_id = body.prompt_id || uuid4();
  if (g) body.comfyfarm_group = g.id;
  const title = runTitle();
  if (title) body.comfyfarm_title = title;
  options.body = JSON.stringify(body);
  submitting = true;
  try {
    for (let attempt = 0; ; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), ATTEMPT_TIMEOUT_MS);
      try {
        const res = await origFetchApi(route, { ...options, signal: ctrl.signal });
        if (res.ok) notifyShell({ type: "comfyfarm:queued" });
        if (res.status !== 503 || attempt >= RETRY_DELAYS_S.length) return res;
      } catch (e) {
        if (attempt >= RETRY_DELAYS_S.length) throw e;
      } finally {
        clearTimeout(timer);
      }
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_S[attempt] * 1000));
    }
  } finally {
    submitting = false;
  }
};

// Inside the dashboard's Workflow tab: tell the ComfyFarm shell (same origin only) so its queue badge updates at once.
function notifyShell(msg) {
  if (window.parent !== window) window.parent.postMessage(msg, location.origin);
}

// ---- group picker next to Run -------------------------------------------------------------------------------------
function closePicker() {
  document.getElementById("comfyfarm-group-picker")?.remove();
}

function openPicker(ev) {
  closePicker();
  const anchor = ev?.currentTarget?.getBoundingClientRect?.() || { left: window.innerWidth - 320, bottom: 60 };
  const cur = storedGroup();
  const box = document.createElement("div");
  box.id = "comfyfarm-group-picker";
  box.style.cssText = "position:fixed;z-index:10000;min-width:220px;padding:6px;border-radius:8px;" +
    "background:var(--comfy-menu-bg,#2b2b2b);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);" +
    "box-shadow:0 6px 24px rgba(0,0,0,.45);font:13px system-ui,sans-serif";
  box.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - 240))}px`;
  box.style.top = `${anchor.bottom + 6}px`;
  // the title of this workflow's runs, above the groups: what the queue shows for them
  const tab = activeTab();
  const tLabel = document.createElement("div");
  tLabel.textContent = tr("Title of these runs");
  tLabel.style.cssText = "padding:4px 8px 4px;opacity:.65;font-size:12px";
  const tInput = document.createElement("input");
  tInput.type = "text";
  tInput.maxLength = 100;
  tInput.value = (tab.key && typedTitles()[tab.key]) || "";
  tInput.placeholder = tab.name ? tr("empty: the tab's name ({name})").replace("{name}", tab.name) : tr("empty: no title");
  tInput.style.cssText = "display:block;width:calc(100% - 16px);margin:0 8px 8px;padding:5px 7px;border-radius:5px;" +
    "border:1px solid var(--border-color,#444);background:var(--comfy-input-bg,#222);color:inherit;font:inherit";
  tInput.oninput = () => { if (tab.key) setTypedTitle(tab.key, tInput.value); };
  tInput.onkeydown = (e) => { if (e.key === "Enter") closePicker(); };
  if (!tab.key) tInput.disabled = true;                   // an older frontend: no workflow to keep it for
  box.append(tLabel, tInput);
  const title = document.createElement("div");
  title.textContent = tr("Worker group for Run");
  title.style.cssText = "padding:4px 8px 6px;opacity:.65;font-size:12px";
  box.append(title);
  const pick = (g) => {
    if ((g?.id ?? null) === (cur?.id ?? null)) return closePicker();
    if (g) localStorage.setItem(GROUP_KEY, JSON.stringify({ id: g.id, name: g.name }));
    else localStorage.removeItem(GROUP_KEY);
    location.reload();          // node definitions are per group (D-20); the open workflow is restored after the reload
  };
  const row = (label, sub, active, onclick) => {
    const r = document.createElement("button");
    r.type = "button";
    r.style.cssText = "display:flex;justify-content:space-between;gap:12px;width:100%;padding:6px 8px;border:0;border-radius:5px;" +
      `background:${active ? "var(--comfy-input-bg,#3a3a3a)" : "transparent"};color:inherit;cursor:pointer;text-align:left;font:inherit`;
    r.onmouseenter = () => (r.style.background = "var(--comfy-input-bg,#3a3a3a)");
    r.onmouseleave = () => (r.style.background = active ? "var(--comfy-input-bg,#3a3a3a)" : "transparent");
    const a = document.createElement("span"); a.textContent = (active ? "✓ " : "") + label;
    const b = document.createElement("span"); b.textContent = sub; b.style.opacity = ".6";
    r.append(a, b);
    r.onclick = onclick;
    return r;
  };
  box.append(row(tr("Any of my groups"), "", !cur, () => pick(null)));
  for (const g of groups) box.append(row(g.name, tr("{n} online").replace("{n}", g.online), cur?.id === g.id, () => pick(g)));
  if (!groups.length) {
    const none = document.createElement("div");
    none.textContent = tr("No worker group available to you.");
    none.style.cssText = "padding:6px 8px;opacity:.7";
    box.append(none);
  }
  document.body.append(box);
  setTimeout(() => {
    const away = (e) => { if (!box.contains(e.target)) { closePicker(); off(); } };
    const esc = (e) => { if (e.key === "Escape") { closePicker(); off(); } };
    const off = () => { document.removeEventListener("mousedown", away, true); document.removeEventListener("keydown", esc, true); };
    document.addEventListener("mousedown", away, true);
    document.addEventListener("keydown", esc, true);
  });
}

// ---- "My queue" next to Run: the user's jobs by title, their state, what the finished ones made ---------------------
const PANEL = "position:fixed;z-index:10000;padding:6px;border-radius:8px;background:var(--comfy-menu-bg,#2b2b2b);" +
  "color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);box-shadow:0 6px 24px rgba(0,0,0,.45);" +
  "font:13px system-ui,sans-serif";
let queueTimer = null;

function closeQueue() {
  clearInterval(queueTimer);
  queueTimer = null;
  document.getElementById("comfyfarm-my-queue")?.remove();
}

function viewUrl(f, extra = "") {
  return "/api/view?" + new URLSearchParams({ filename: f.filename, subfolder: f.subfolder || "", type: f.type || "output" }) + extra;
}

function thumb(j) {
  const box = document.createElement("div");
  box.style.cssText = "width:56px;height:42px;flex:none;border-radius:4px;overflow:hidden;background:var(--comfy-input-bg,#222);" +
    "display:flex;align-items:center;justify-content:center;opacity:.95";
  const f = j.preview;
  if (!f) {
    box.textContent = j.status === "failed" ? "✕" : j.place != null ? "…" : j.status === "running" ? "▶" : "–";
    box.style.opacity = ".5";
    return box;
  }
  const video = /\.(mp4|webm|mov|mkv)$/i.test(f.filename);
  const el = document.createElement(video ? "video" : "img");
  if (video) { el.muted = true; el.preload = "metadata"; el.src = viewUrl(f) + "#t=0.1"; }
  else { el.loading = "lazy"; el.src = viewUrl(f, /\.gif$/i.test(f.filename) ? "" : "&preview=webp;60"); el.alt = f.filename; }
  el.style.cssText = "width:100%;height:100%;object-fit:cover";
  const a = document.createElement("a");
  a.href = viewUrl(f); a.target = "_blank"; a.rel = "noopener"; a.title = f.filename;
  a.style.cssText = "display:contents";
  a.append(el);
  box.append(a);
  return box;
}

function progressOf(p) {
  const s = p?.state;
  if (!s || typeof s !== "object") return null;
  if (typeof s.max === "number" && s.max > 0) return s.value / s.max;
  const nodes = Object.values(s).filter((n) => n && typeof n === "object");
  if (!nodes.length) return null;
  return nodes.reduce((a, n) => a + (n.state === "finished" ? 1 : n.max ? (n.value || 0) / n.max : 0), 0) / nodes.length;
}

function jobRow(j) {
  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:10px;align-items:center;padding:6px 8px;border-radius:5px";
  const text = document.createElement("div");
  text.style.cssText = "min-width:0;flex:1";
  const t1 = document.createElement("div");
  t1.textContent = j.title || j.hint || `#${j.prompt_id.slice(0, 8)}`;
  t1.style.cssText = "font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
  const t2 = document.createElement("div");
  t2.textContent = j.title && j.hint ? j.hint : "";
  t2.style.cssText = "opacity:.6;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
  const t3 = document.createElement("div");
  const p = j.status === "running" ? progressOf(j.progress) : null;
  t3.textContent = [tr(j.status), j.place != null ? tr("#{n} in line").replace("{n}", j.place) : "",
                    p != null ? `${Math.round(p * 100)}%` : "", j.worker || "", j.error || ""].filter(Boolean).join(" · ");
  t3.style.cssText = `font-size:12px;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;${j.status === "failed" ? "color:#e57373;opacity:1" : ""}`;
  text.append(t1, ...(t2.textContent ? [t2] : []), t3);
  row.title = [j.title, j.hint, j.prompt_id].filter(Boolean).join("\n");
  row.append(thumb(j), text);
  return row;
}

function openQueue(ev) {
  closeQueue();
  closePicker();
  const anchor = ev?.currentTarget?.getBoundingClientRect?.() || { left: window.innerWidth - 380, bottom: 60 };
  const box = document.createElement("div");
  box.id = "comfyfarm-my-queue";
  box.style.cssText = PANEL + ";width:360px;max-height:70vh;overflow:auto";
  box.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - 372))}px`;
  box.style.top = `${anchor.bottom + 6}px`;
  const list = document.createElement("div");
  const foot = document.createElement("a");
  foot.textContent = tr("Open in the dashboard");
  foot.href = "/comfyfarm/dashboard/#/queue"; foot.target = "_blank"; foot.rel = "noopener";
  foot.style.cssText = "display:block;padding:6px 8px 2px;font-size:12px;opacity:.75;color:inherit";
  box.append(list, foot);
  document.body.append(box);
  let sig = "";
  const head = (s) => {
    const d = document.createElement("div");
    d.textContent = s;
    d.style.cssText = "padding:6px 8px 2px;opacity:.6;font-size:12px";
    return d;
  };
  async function load() {
    try {
      const r = await origFetchApi("/farm/mine?recent=8");
      if (!r.ok) return;
      const d = await r.json();
      const next = JSON.stringify(d);
      if (next === sig) return;
      sig = next;
      const parts = [];
      if (d.queue.length) parts.push(head(tr("Running")), ...d.queue.map(jobRow));
      if (d.recent.length) parts.push(head(tr("Finished")), ...d.recent.map(jobRow));
      if (!parts.length) parts.push(head(tr("Nothing queued, nothing finished yet.")));
      list.replaceChildren(...parts);
    } catch {
      /* the router is away for a moment: the list stays */
    }
  }
  load();
  queueTimer = setInterval(() => (document.getElementById("comfyfarm-my-queue") ? load() : closeQueue()), 2000);
  setTimeout(() => {
    const away = (e) => { if (!box.contains(e.target)) { closeQueue(); off(); } };
    const esc = (e) => { if (e.key === "Escape") { closeQueue(); off(); } };
    const off = () => { document.removeEventListener("mousedown", away, true); document.removeEventListener("keydown", esc, true); };
    document.addEventListener("mousedown", away, true);
    document.addEventListener("keydown", esc, true);
  });
}

app.registerExtension({
  name: "comfyfarm.ui",
  async init() {
    // `init` runs before the node definitions are fetched: a group the user may no longer use is dropped here, so
    // the canvas never shows nodes of a group that submissions would be refused for
    try {
      const r = await origFetchApi("/farm/groups");
      groups = r.ok ? await r.json() : [];
    } catch {
      groups = [];
    }
    const g = storedGroup();
    if (g && !groups.some((x) => x.id === g.id)) localStorage.removeItem(GROUP_KEY);
    const cur = storedGroup();
    app.registerExtension({         // registered now, with the validated name (the action bar reads it once)
      name: "comfyfarm.ui.group",
      actionBarButtons: [{
        icon: "icon-[lucide--server]",
        label: `${tr("Group")}: ${cur ? cur.name : tr("any")}`,
        tooltip: tr("Worker group for Run — the node list follows it"),
        onClick: openPicker,
      }, {
        icon: "icon-[lucide--list]",
        label: tr("My queue"),
        tooltip: tr("My jobs: titles, state, results"),
        onClick: openQueue,
      }],
    });
  },
});
