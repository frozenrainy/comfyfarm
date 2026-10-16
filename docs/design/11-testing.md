# 11 — Testing

Most failures this design defends against are timing and partial-failure problems that unit tests do not reach. Three
layers:

## 1. Protocol probe (real ComfyUI, no GPU needed)

`spikes/protocol_probe.py <url>` checks every fact in [04](04-worker-protocol.md) against a running ComfyUI using tiny
core-node workflows. Run it against each ComfyUI version we support; its output is committed under `docs/research/`. The probe refuses to run
against a worker whose queue is not empty: one early run overlapped a previous run's slow job and produced four false
FAILs (wrong job running, events arriving late) — results against a shared worker are meaningless.
Every check that can pass trivially has a control: e.g. the reconnect-race check (S3) is paired with a single-socket
control (S3b) that must receive events — otherwise "0 events" could just mean "no events at all".

## 2. Fake worker (protocol-accurate)

A small aiohttp server implementing `/prompt`, `/queue`, `/api/jobs[/{id}]`, `/history`, `/interrupt`, `/object_info`,
`/system_stats` and `/ws`, with **the real protocol semantics** (events only to the job's `client_id` socket, P10;
duplicate `prompt_id` re-executes, P3; canonical-UUID check, P2; cooperative interrupt, P7). A fake that broadcasts
progress to every socket once produced a non-existent "reconnect storm" in someone else's lab — a fake worker that is
wrong is worse than none. Its behaviour is checked against the probe: the probe must give identical results against
the fake and against real ComfyUI.

Fault modes (per worker, switchable at runtime):

| Mode | Behaviour | Router branch it exercises |
|---|---|---|
| `hang` | records the job, answers the POST after 30 s | Ambiguous → ask worker → accepted |
| `drop` | answers 400 with `node_errors` | Rejected → failed, no re-dispatch loop |
| `deaf` | POST and job lookups stall, **`/queue` stays healthy** | Ambiguous → cannot tell → stays dispatching → lost-job judgement (if `/queue` also stalled, the worker would just be marked offline and the branch would never be reached) |
| `block` | whole event loop stalls for N s (model load) | busy vs dead, WS ping timeout |
| `amnesia` | restart: forgets queue and history | lost job → failed |
| `slow-interrupt` | interrupt takes effect after N s | cancelling state |
| `old-ws` | no reconnect fix | progress freeze after router restart is detected and reported |

## 3. Scenario tests (router + Postgres + N fake workers)

Every job carries a unique tag; the judge checks **lost / duplicated / ghost / state mismatch** per job, not just "it
finished". Scenarios: router kill -9 mid-dispatch; worker restart mid-job; network partition between router and a
worker; Postgres restart; rolling restart under load; HA failover (when the HA module exists).

**Distrust the judge.** Each scenario runs with a positive and a negative control: a deliberately injected duplicate
must be reported as a duplicate; a deliberately dropped job must be reported as lost. A judge that never fails is the
most dangerous component in the suite.

Practicalities: change the seed for every job (identical prompts are served from the worker cache and emit no progress
events), and never let two routers share one test database.

## 4. Real-GPU acceptance (test box)

Once the test box exists: the probe against both workers, then a 2-worker farm (CUDA + ROCm) with real video workflows,
including the measurements listed in [10](10-heterogeneous-gpus.md).
