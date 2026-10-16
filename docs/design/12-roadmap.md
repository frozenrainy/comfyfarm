# 12 — Roadmap

| Milestone | Content | Needs GPU box |
|---|---|---|
| **M0 Design** (done) | design docs, protocol probe, worker-extension prototype (reconnect fix verified on 0.41.0) | no |
| **M1 Core** (done) | schema + migrations, dispatcher (`SKIP LOCKED`), adapter with classified outcomes, reconcile + lost jobs, health, cancel, admin API, capability requirements, NOTIFY wake-up, fake worker + scenario tests (19 tests incl. fake↔real parity, mutation-checked double dispatch) | no — fake workers + CPU ComfyUI |
| **M2 Events & gateway** (done) | worker WS relay, catch-up, status broadcast, gateway routing, identity providers (`local`, `trusted-header`, `dev`), frontend extension (group picker, idempotent submit), pre-submit validation. Accepted by a browser E2E: stock frontend → gateway → router → R9700 worker, preview shown | partly — real previews need GPU |
| **M3 Dashboard & monitoring** (done) | dashboard (groups, queue, workers, recent; no build step, D-32), pause/resume/reorder/retry/notes, admin cancel, `/api/health/dispatch` aligned with the dispatcher + Prometheus `/metrics` (D-33). Browser E2E through the gateway | no |
| **M4 Rollout** | drain/restart/verify, restart backends, `comfy-upgrade/1` render + badge, recycle policy | yes |
| **M5 Heterogeneous acceptance** | CUDA + ROCm farm on the test box, measurements from doc 10, first public release | yes |
| later | HA module, OIDC, Prometheus dashboards, container images | — |

Publication: the repository goes public at the end of M1 at the earliest (working code + docs), after a secret scan
and a review of every document against the export rules (no internal names, hosts, organisations or credentials).
