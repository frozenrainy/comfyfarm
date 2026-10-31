// Profiles from a recipe start from the build's template — which knows nothing of the flags and environment an
// operator added to the profile the workers run now. Field (dev.25–27): two builds in a row made GPU profiles without
// --disable-smart-memory --disable-async-offload --disable-pinned-memory; the next rollout dropped them, and the worker
// hit GPU faults (illegal memory access as models were moved) for two days. The new profile carries them over.
// No imports: tested on its own (tests/test_browser_code.py).

const ALLOW = "COMFYFARM_ALLOW";              // its own field in the dialog

/** The profile the workers of `role` ("gpu" | "ui") run now: the one most of their instances are assigned to
 *  (the newest on a tie), or null. */
export function currentProfile(role, insts, profiles) {
  const count = new Map();
  for (const i of insts || []) {
    const inst = i && i.instance;
    if (!inst || !inst.profile_id || (inst.role || "gpu") !== role) continue;
    count.set(inst.profile_id, (count.get(inst.profile_id) || 0) + 1);
  }
  let best = null;
  for (const [pid, n] of count) {
    if (!best || n > best.n || (n === best.n && pid > best.pid)) best = { pid, n };
  }
  return best ? (profiles || []).find((p) => p.id === best.pid) || null : null;
}

/** What `cur` has beyond the template's half (`tpl`): its other flags, and environment the template lacks or sets
 *  differently. COMFYFARM_ALLOW comes back separately. */
export function carried(cur, tpl) {
  if (!cur) return { flags: [], env: {}, allow: "" };
  const tflags = new Set((tpl && tpl.flags) || []), tenv = (tpl && tpl.env) || {};
  const env = {};
  for (const [k, v] of Object.entries(cur.env || {})) {
    if (k !== ALLOW && tenv[k] !== v) env[k] = v;
  }
  return { flags: (cur.flags || []).filter((f) => !tflags.has(f)), env, allow: (cur.env || {})[ALLOW] || "" };
}

/** The new profile's flags and environment: the template's, then what is carried; `allow` (when given) last. */
export function merged(tpl, extra, allow) {
  const flags = [...((tpl && tpl.flags) || [])];
  for (const f of (extra && extra.flags) || []) if (!flags.includes(f)) flags.push(f);
  const env = { ...((tpl && tpl.env) || {}), ...((extra && extra.env) || {}) };
  if (allow && allow.trim()) env[ALLOW] = allow.trim();
  return { flags, env };
}

/** Text fields of the dialog ↔ values. */
export const flagsText = (flags) => (flags || []).join(" ");
export const envText = (env) => Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join("\n");
export const parseFlags = (s) => (s || "").split(/\s+/).filter(Boolean);
export function parseEnv(s) {
  return Object.fromEntries((s || "").split("\n").map((l) => l.trim()).filter((l) => l && l.includes("="))
    .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1)]));
}
