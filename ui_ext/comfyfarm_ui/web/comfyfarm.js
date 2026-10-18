// SPDX-License-Identifier: GPL-3.0-or-later
// ComfyFarm frontend extension (see ../__init__.py). Official extension API only — no frontend fork.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const GROUP_KEY = "comfyfarm.workflowGroup";                // {id, name} in localStorage; absent = the site default
const RETRY_DELAYS_S = [1, 2, 3, 4, 5, 5, 5, 5, 5, 5];   // D-23: retry the same prompt_id, never a new one
const ATTEMPT_TIMEOUT_MS = 20000;                          // stock fetch has no timeout; a dead primary hangs minutes

let groups = [];
let submitting = false;

function storedGroup() {
  try {
    const g = JSON.parse(localStorage.getItem(GROUP_KEY) || "null");
    return g && typeof g.id === "number" && typeof g.name === "string" ? g : null;
  } catch {
    return null;
  }
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
  body.prompt_id = body.prompt_id || crypto.randomUUID();
  if (g) body.comfyfarm_group = g.id;
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
  const title = document.createElement("div");
  title.textContent = "Worker group for Run";
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
  box.append(row("Site default", "", !cur, () => pick(null)));
  for (const g of groups) box.append(row(g.name, `${g.online} online`, cur?.id === g.id, () => pick(g)));
  if (!groups.length) {
    const none = document.createElement("div");
    none.textContent = "No worker group available to you.";
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
        label: `Group: ${cur ? cur.name : "default"}`,
        tooltip: "Worker group for Run — the node list follows it",
        onClick: openPicker,
      }],
    });
  },
});
