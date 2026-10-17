// Shared helpers for the web UI pages. No framework, no build step (D-32).
export const $ = (s, root = document) => root.querySelector(s);
export const LOGIN = "/comfyfarm/login?next=/comfyfarm/dashboard/";
export const state = { me: null, clockSkew: 0 };

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}
export function setText(el, text) { text = text ?? ""; if (el.textContent !== String(text)) el.textContent = text; }
export function pill(state_, label) { return h("span", { class: `pill s-${state_}` }, label ?? state_.replace("_", " ")); }
export function setPill(el, st, label) {
  const sig = `${st}|${label ?? ""}`;
  if (el.dataset.v !== sig) { el.dataset.v = sig; el.replaceChildren(pill(st, label)); }
}

// ---- time ------------------------------------------------------------------------------------------------------
export function now() { return Date.now() + state.clockSkew; }
export function ago(iso) { return iso ? (now() - Date.parse(iso)) / 1000 : null; }
export function dur(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
export function hours(s) { return s == null ? "—" : s < 3600 ? dur(s) : `${(s / 3600).toFixed(1)} h`; }
export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), a = ago(iso);
  if (a != null && a < 86400) return `${dur(a)} ago`;
  return d.toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
export const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

// ---- progress --------------------------------------------------------------------------------------------------
export function progressOf(p) {
  const s = p?.state;
  if (!s || typeof s !== "object") return null;
  if (typeof s.max === "number" && s.max > 0) return s.value / s.max;
  const nodes = Object.values(s).filter((n) => n && typeof n === "object");     // per-node progress_state
  if (!nodes.length) return null;
  const done = nodes.reduce((a, n) => a + (n.state === "finished" ? 1 : n.max ? (n.value || 0) / n.max : 0), 0);
  return done / nodes.length;
}
export function bar(frac, cls = "") {
  if (frac == null) return "";
  const pct = Math.round(Math.min(1, Math.max(0, frac)) * 100);
  const i = h("i"); i.style.width = pct + "%";
  return h("span", { class: "barwrap" }, h("span", { class: `bar ${cls}` }, i), ` ${pct}%`);
}
export function setBar(el, frac) {
  const key = frac == null ? "" : String(Math.round(frac * 100));
  if (el.dataset.v !== key) { el.dataset.v = key; el.replaceChildren(bar(frac)); }
}

// ---- feedback --------------------------------------------------------------------------------------------------
let toastTimer;
export function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 4500);
}

// ---- HTTP ------------------------------------------------------------------------------------------------------
export class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
export async function api(method, path, body) {
  let r;
  try {
    r = await fetch(path, {
      method, credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) { throw new ApiError(`cannot reach the server (${e.message})`, 0); }
  if (r.status === 401) { location.href = LOGIN; throw new ApiError("not logged in", 401); }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const j = await r.json(); const d = j.detail ?? j.error;
      if (typeof d === "string") msg = d; else if (d?.message) msg = d.message;
    } catch {}
    throw new ApiError(msg, r.status);
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : null;
}
// Run an action, report the outcome, then let the page refresh.
export async function act(fn, okMsg, after) {
  try {
    const res = await fn();
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(res) : okMsg);
    return res;
  } catch (e) { toast(e.message, true); }
  finally { after?.(); }
}
export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) u.set(k, v);
  return u.toString();
}

// ---- rows ------------------------------------------------------------------------------------------------------
// Reconcile <tbody> rows by key: drop the gone ones first, then reuse and reorder only when needed. (Dropping last
// would make every row below a finished job "out of place" and get moved — and a moved row loses keyboard focus.)
export function syncRows(tbody, items, key, build, update) {
  const keep = new Set(items.map((it) => String(key(it))));
  for (const tr of [...tbody.children]) if (!keep.has(tr.dataset.key)) tr.remove();
  const existing = new Map([...tbody.children].map((tr) => [tr.dataset.key, tr]));
  let prev = null;
  for (const it of items) {
    const k = String(key(it));
    let tr = existing.get(k);
    if (!tr) { tr = build(it); tr.dataset.key = k; }
    tr._item = it;
    update(tr, it);
    const want = prev ? prev.nextSibling : tbody.firstChild;
    if (tr !== want) tbody.insertBefore(tr, want);
    prev = tr;
  }
}
// A row whose cells are addressed by name: row.c.status, row.c.owner, …
export function namedRow(names, classes = {}) {
  const cells = names.map((n) => h("td", { class: classes[n] || "" }));
  const tr = h("tr", {}, ...cells);
  tr.c = Object.fromEntries(cells.map((td, i) => [names[i], td]));
  return tr;
}
export function table(headers, classes = {}) {
  const tbody = h("tbody");
  const el = h("div", { class: "scroll" },
    h("table", {}, h("thead", {}, h("tr", {}, ...headers.map(([label, cls]) => h("th", { class: cls || "" }, label)))), tbody));
  return { el, tbody };
}

export function noteInput(save) {
  const inp = h("input", { class: "note", placeholder: "note", maxlength: "500" });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") inp.blur();
    if (e.key === "Escape") { inp.value = inp.dataset.saved || ""; inp.blur(); }
  });
  inp.addEventListener("change", () => save(inp.value));
  return inp;
}
export function syncNote(inp, value) {
  inp.dataset.saved = value || "";
  if (document.activeElement !== inp) inp.value = value || "";
}
export function button(label, onclick, opts = {}) {
  return h("button", { type: "button", onclick, title: opts.title, class: opts.cls }, label);
}

// ---- dialogs ---------------------------------------------------------------------------------------------------
// fields: [{name, label, type: text|password|number|checkbox|checkboxes|select, value, options:[{value,label}], hint}]
export function formDialog(title, fields, submitLabel = "Save") {
  return new Promise((resolve) => {
    const inputs = {};
    const rows = fields.map((f) => {
      let control;
      if (f.type === "checkboxes") {
        const boxes = (f.options || []).map((o) => {
          const cb = h("input", { type: "checkbox", value: o.value, checked: (f.value || []).includes(o.value) });
          return { cb, el: h("label", { class: "check" }, cb, ` ${o.label}`) };
        });
        inputs[f.name] = () => boxes.filter((b) => b.cb.checked).map((b) => b.cb.value);
        control = h("div", { class: "checks" }, ...boxes.map((b) => b.el), boxes.length ? null : h("span", { class: "muted" }, "none available"));
      } else if (f.type === "checkbox") {
        const cb = h("input", { type: "checkbox", checked: f.value });
        inputs[f.name] = () => cb.checked;
        control = h("label", { class: "check" }, cb, ` ${f.text || ""}`);
      } else if (f.type === "select") {
        const sel = h("select", {}, ...(f.options || []).map((o) => h("option", { value: o.value }, o.label)));
        sel.value = f.value ?? "";
        inputs[f.name] = () => sel.value;
        control = sel;
      } else {
        const inp = h("input", { type: f.type || "text", value: f.value ?? "", placeholder: f.placeholder, autocomplete: f.autocomplete || "off",
                                 min: f.min, step: f.step });
        inputs[f.name] = () => (f.type === "number" ? (inp.value === "" ? null : Number(inp.value)) : inp.value);
        control = inp;
      }
      return h("div", { class: "field" }, h("div", { class: "label" }, f.label), control, f.hint ? h("div", { class: "hint" }, f.hint) : null);
    });
    const dlg = h("dialog", { class: "form" });
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const form = h("form", { method: "dialog", onsubmit: (e) => {
      e.preventDefault();
      done(Object.fromEntries(Object.entries(inputs).map(([k, get]) => [k, get()])));
    } }, h("h3", {}, title), ...rows,
      h("div", { class: "dialog-actions" }, button("Cancel", () => done(null)), h("button", { type: "submit", class: "primary" }, submitLabel)));
    dlg.append(form);
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal();
    form.querySelector("input:not([type=checkbox]), select")?.focus();
  });
}

// ---- theme -----------------------------------------------------------------------------------------------------
export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("comfyfarm.theme", theme);     // read before first paint by index.html
}

// ---- small charts ----------------------------------------------------------------------------------------------
// Daily stacked bars (completed / failed / cancelled) — enough to see a trend without a chart library.
export function dailyChart(daily) {
  const max = Math.max(1, ...daily.map((d) => d.completed + d.failed + d.cancelled));
  const cols = daily.map((d) => {
    const total = d.completed + d.failed + d.cancelled;
    const seg = (n, cls) => { const s = h("i", { class: cls }); s.style.height = `${(n / max) * 100}%`; return s; };
    return h("div", { class: "col", title: `${d.date}: ${d.completed} done, ${d.failed} failed, ${d.cancelled} cancelled` },
      h("div", { class: "stack" }, seg(d.cancelled, "c"), seg(d.failed, "f"), seg(d.completed, "ok")),
      h("div", { class: "x" }, daily.length <= 10 || total ? d.date.slice(5) : ""));
  });
  return h("div", { class: "chart" }, ...cols);
}
export function statCards(t) {
  const card = (label, value, cls = "") => h("div", { class: `stat ${cls}` }, h("div", { class: "v" }, value), h("div", { class: "k" }, label));
  return h("div", { class: "stats" },
    card("jobs finished", String(t.jobs)),
    card("completed", String(t.completed), "ok"),
    card("failed", String(t.failed), t.failed ? "bad" : ""),
    card("cancelled", String(t.cancelled)),
    card("GPU time", hours(t.gpu_seconds)),
    card("avg wait", dur(t.avg_wait_seconds)),
    card("avg run", dur(t.avg_run_seconds)));
}
// Period picker: presets + custom dates, at most 30 days (the server enforces the same limit).
export function periodPicker(initial, onchange) {
  const MAX = 30;
  let cur = { ...initial };
  const today = new Date().toLocaleDateString("sv");           // YYYY-MM-DD in local time
  const presets = [["24 h", 1], ["7 days", 7], ["30 days", 30]].map(([label, days]) =>
    h("button", { type: "button", class: "seg", onclick: () => { cur = { days }; sync(); onchange(cur); } }, label));
  const from = h("input", { type: "date", max: today }), to = h("input", { type: "date", max: today });
  const apply = h("button", { type: "button", onclick: () => {
    if (!from.value || !to.value) return toast("pick both dates", true);
    const span = (Date.parse(to.value) - Date.parse(from.value)) / 86400000 + 1;
    if (span < 1) return toast("end is before start", true);
    if (span > MAX) return toast(`at most ${MAX} days`, true);
    cur = { start: from.value, end: to.value }; sync(); onchange(cur);
  } }, "Apply");
  function sync() {
    presets.forEach((b, i) => b.classList.toggle("on", cur.days === [1, 7, 30][i]));
    if (cur.start) { from.value = cur.start; to.value = cur.end; }
  }
  sync();
  return h("div", { class: "period" }, h("div", { class: "segs" }, ...presets),
    h("span", { class: "custom" }, from, " – ", to, apply, h("span", { class: "muted" }, ` max ${MAX} days`)));
}
