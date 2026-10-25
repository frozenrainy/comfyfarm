// Progress and ETA of a running job, from ComfyUI's per-node progress_state and the router's plan (core.jobs.job_plan:
// a weight per node in sampler steps). No imports: tests/test_browser_code.py runs this file under node.
//
// progress: the row's `progress` field, {state: {<node id>: {value, max, state}}, at: <unix seconds of that report>}
//           (or {state: {value, max, node}} from a bare progress event — one number, weighed as the one node);
// plan:     {<node id>: weight}; a node ComfyUI reports that the plan does not know weighs 0.1.
const FALLBACK = 0.1;

// "12:3" — a node inside an expanded graph (a subgraph, a loop body) — belongs to its parent 12 for the weighing: the
// plan knows the parent only, and parent and children must not count twice.
const baseOf = (id) => String(id).split(":")[0];

// {frac, remaining (in steps), current: {id, value, max} | null, started: bool}
export function weighted(progress, plan) {
  const s = progress?.state;
  if (!s || typeof s !== "object") return { frac: null, remaining: null, current: null, started: false };
  if (typeof s.max === "number" && s.max > 0) {          // a bare progress event: one node, nothing to weigh against
    return { frac: s.value / s.max, remaining: null, current: { id: String(s.node ?? ""), value: s.value, max: s.max }, started: true };
  }
  const seen = {};                                       // base id → the nodes ComfyUI reported under it
  for (const [id, n] of Object.entries(s)) if (n && typeof n === "object") (seen[baseOf(id)] ??= []).push({ id, ...n });
  if (!Object.keys(seen).length) return { frac: null, remaining: null, current: null, started: false };
  let done = 0, total = 0, current = null;
  for (const base of new Set([...Object.keys(plan || {}), ...Object.keys(seen)])) {
    const w = plan ? (base in plan ? plan[base] : FALLBACK) : 1;
    total += w;
    const nodes = seen[base];
    if (!nodes) continue;                                  // in the plan, not reported yet: still to come
    let part = 0;
    for (const n of nodes) {
      if (n.state === "finished") part += 1;
      else if (n.max > 0) {
        part += (n.value || 0) / n.max;
        if (n.state === "running" && (current === null || n.max > 1)) current = { id: n.id, value: n.value || 0, max: n.max };
      }
    }
    done += w * part / nodes.length;
  }
  return { frac: total > 0 ? Math.min(1, done / total) : null, remaining: total > 0 ? Math.max(0, total - done) : null, current, started: true };
}

// Seconds per step of the current node, from two of its reports: samples [{at, id, value, max}] (newest last).
export function pace(samples) {
  for (let i = samples.length - 1; i > 0; i--) {
    const b = samples[i], a = samples[i - 1];
    if (a.id === b.id && a.max === b.max && b.value > a.value && b.at > a.at) return (b.at - a.at) / (b.value - a.value);
  }
  return null;
}

// Seconds left: the steps left at the pace of the sampler at work. null while nothing paces it (loading, the first
// step, a node without steps) — the caller says so instead of a number.
export function eta(w, secondsPerStep) {
  if (w.remaining == null || secondsPerStep == null || !(w.current?.max > 1)) return null;
  return Math.max(0, w.remaining * secondsPerStep);
}

// How long since the worker last said anything (seconds), null without a report: a running job with no new word for
// a while is not at 0 %, it is out of touch (the editor shows the same number for both otherwise).
export function silence(progress, nowSeconds) {
  return typeof progress?.at === "number" ? Math.max(0, nowSeconds - progress.at) : null;
}
