// Progress and time left of a running job (docs/design/05-events.md), from what the router sends with it:
//   job.progress  {state: {<node id>: {value, max, state}}, at: unix s the numbers last moved,
//                  steps: {<node id>: [unix s, value] of its first step}, loops: {<start id>: {n, ends: [unix s …]}}}
//                 (state may be a bare {value, max, node} from an old progress event)
//   job.plan      {<prompt node id>: weight in sampler steps} (core.jobs.job_plan; 0 = did not run last time)
//   job.loops     {<StartLoop id>: {end, body: [prompt node ids], n}} (core.jobs.job_loops)
// No imports: tests/test_browser_code.py runs this file under node.
const FALLBACK = 0.1;
const QUIET_MIN = 120, QUIET_LOADING = 600;     // seconds: a step's silence is normal; before the first, models load

// A loop iteration's copy of node 160:137 is reported as "96.0.0.3_160:137": start 96, iteration 3. Nested loops
// nest the prefix; the outermost loop and the innermost prompt node are what the weighing needs.
const EXPANDED_OUTER = /^([^.]+)\.\d+\.\d+\.(\d+)_(.+)$/;
const EXPANDED_INNER = /^.*\.\d+\.\d+\.\d+_(.+)$/;
const MACHINERY = /^[^.]+\.\d+\.\d+\.(iteration_|progress_|result)/;   // the loop's own bookkeeping nodes
export const promptNode = (id) => (EXPANDED_INNER.exec(id) || [null, id])[1];
export function copyOf(id) {
  const m = EXPANDED_OUTER.exec(id);
  return m ? { start: m[1], pos: Number(m[2]) } : null;
}

const part = (n) => n.state === "finished" ? 1 : n.max > 0 ? (n.value || 0) / n.max : 0;

// {frac, remaining (steps), current: {id, value, max} | null, started, loop: {k, n, known} | null, loopLeft (s) | null}
export function weighted(progress, plan, loops = {}, nowSeconds = null) {
  const none = { frac: null, remaining: null, current: null, started: false, loop: null, loopLeft: null };
  const s = progress?.state;
  if (!s || typeof s !== "object") return none;
  if (typeof s.max === "number" && s.max > 0) {          // a bare progress event: one node, nothing to weigh against
    return { ...none, frac: s.value / s.max, current: { id: String(s.node ?? ""), value: s.value, max: s.max }, started: true };
  }
  const reported = Object.entries(s).filter(([, n]) => n && typeof n === "object");
  if (!reported.length) return none;
  plan = plan || {};
  const weightOf = (orig, n) => {                        // ComfyUI's own step count beats the guess from the prompt
    const w = orig in plan ? plan[orig] : FALLBACK;
    return w >= 1 && n?.max > 1 ? n.max : w;
  };
  const bodyOf = {};                                     // prompt node → its outermost loop
  const inner = new Set(Object.values(loops || {}).flatMap((l) => l.body || []));
  for (const [start, l] of Object.entries(loops || {})) if (!inner.has(start)) for (const b of l.body || []) bodyOf[b] = start;

  let done = 0, total = 0, current = null, firstStep = -1;
  const perLoop = {};                                    // start → {pos → [{orig, n}]}
  const direct = {};
  for (const [id, n] of reported) {
    if (MACHINERY.test(id)) continue;
    const c = copyOf(id);
    if (c && c.start in (loops || {})) (perLoop[c.start] ??= {})[c.pos] ??= [], perLoop[c.start][c.pos].push({ orig: promptNode(id), n });
    else direct[id] = n;
    if (n.state === "running" && n.max > 1) {            // the sampler at work: the one whose first step came last
      const t = progress.steps?.[id]?.[0] ?? 0;
      if (current === null || t >= firstStep) { current = { id, value: n.value || 0, max: n.max }; firstStep = t; }
    }
  }
  // outside loops: the plan's nodes, done as reported (a body's template ids are reported too — its copies count)
  for (const orig of Object.keys(plan)) {
    if (orig in bodyOf) continue;
    const n = direct[orig], w = weightOf(orig, n);
    total += w;
    if (n) done += w * part(n);
  }
  // loops: finished iterations whole, the current one node by node, the rest still to come
  let loop = null, loopLeft = null;
  for (const [start, l] of Object.entries(loops || {})) {
    if (inner.has(start)) continue;
    const said = progress.loops?.[start], seen = perLoop[start] || {};
    const positions = Object.keys(seen).map(Number);
    const n = said?.n ?? l.n ?? (positions.length ? Math.max(...positions) + 1 : null);
    const ended = said ? Math.max(0, said.ends.length - 1) : 0;    // ends[0]: the loop started
    const sample = Object.fromEntries((seen[ended] || seen[0] || []).map((x) => [x.orig, x.n]));
    const per = (l.body || []).reduce((a, b) => a + weightOf(b, sample[b]), 0);
    const iters = n ?? Math.max(1, ended + 1);
    total += iters * per;
    const now = (seen[ended] || []).reduce((a, x) => a + weightOf(x.orig, x.n) * part(x.n), 0);
    done += ended * per + Math.min(per, now);
    if (!loop || positions.length) loop = { k: Math.min(ended + 1, iters), n, known: (said?.n ?? l.n) != null };
    if (said && said.ends.length >= 2 && n != null && nowSeconds != null) {
      const e = said.ends, d = e[e.length - 1] - e[e.length - 2];         // the last iteration's length
      loopLeft = Math.max(0, d - (nowSeconds - e[e.length - 1])) + Math.max(0, n - ended - 1) * d;
    }
  }
  return { frac: total > 0 ? Math.min(1, done / total) : null, remaining: total > 0 ? Math.max(0, total - done) : null,
           current, started: true, loop, loopLeft };
}

// Seconds per step of the sampler at work: the router's first step report of it against the latest (the model's
// loading before step 1 left out), else two reports this page saw. samples: [{at, id, value, max}], newest last.
export function pace(progress, current, samples = []) {
  const first = current && progress?.steps?.[current.id];
  if (first && current.value > first[1] && progress.at > first[0]) return (progress.at - first[0]) / (current.value - first[1]);
  for (let i = samples.length - 1; i > 0; i--) {
    const b = samples[i], a = samples[i - 1];
    if (a.id === b.id && a.max === b.max && b.value > a.value && b.at > a.at) return (b.at - a.at) / (b.value - a.value);
  }
  return null;
}

// Seconds left: a loop that has run an iteration — its last iteration's length for each one left; else the steps
// left at the pace of the sampler at work. null while nothing paces it — and through a loop's first iteration: its
// samplers do not run at one pace (a two-pass video's second pass takes half as long a step), so no number until
// one whole iteration has been timed.
export function eta(w, secondsPerStep) {
  if (w.loopLeft != null) return w.loopLeft;
  if (w.loop) return null;
  if (w.remaining == null || secondsPerStep == null || !(w.current?.max > 1)) return null;
  return Math.max(0, w.remaining * secondsPerStep);
}

// How long the numbers have not moved (s), and whether that is longer than this job's own rhythm: 2.5 times the
// step pace, at least 2 min — before the first step 10 min (models load). A 3-minute step is a 3-minute silence.
export function silence(progress, nowSeconds, secondsPerStep = null) {
  if (typeof progress?.at !== "number") return { quiet: null, alarming: false };
  const quiet = Math.max(0, nowSeconds - progress.at);
  const limit = secondsPerStep != null ? Math.max(QUIET_MIN, 2.5 * secondsPerStep) : QUIET_LOADING;
  return { quiet, alarming: quiet > limit };
}
