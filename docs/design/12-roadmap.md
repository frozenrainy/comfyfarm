# 12 — Roadmap

| Milestone | Content | Needs GPU box |
|---|---|---|
| **M0 Design** (done) | design docs, protocol probe, worker-extension prototype (reconnect fix verified on 0.41.0) | no |
| **M1 Core** (done) | schema + migrations, dispatcher (`SKIP LOCKED`), adapter with classified outcomes, reconcile + lost jobs, health, cancel, admin API, capability requirements, NOTIFY wake-up, fake worker + scenario tests (19 tests incl. fake↔real parity, mutation-checked double dispatch) | no — fake workers + CPU ComfyUI |
| **M2 Events & gateway** (done) | worker WS relay, catch-up, status broadcast, gateway routing, identity providers (`local`, `trusted-header`, `dev`), frontend extension (group picker, idempotent submit), pre-submit validation. Accepted by a browser E2E: stock frontend → gateway → router → R9700 worker, preview shown | partly — real previews need GPU |
| **M3 Dashboard & monitoring** (done) | dashboard (groups, queue, workers, recent; no build step, D-32), pause/resume/reorder/retry/notes, admin cancel, `/api/health/dispatch` aligned with the dispatcher + Prometheus `/metrics` (D-33). Browser E2E through the gateway | no |
| **M4a Accounts & web UI** (done) | dashboard / queue / history / settings / system pages (13), user accounts with group permissions (D-34), confirmed sessions (D-35), light/dark/auto theme, statistics up to 30 days | no |
| **M4b Rollout** (done) | profiles + instances, render (§3) + badge (§5), launcher / restart agent / artifact publisher (stdlib, worker side), backends manual · ssh · http-agent · systemd-local, rollout engine (drain, seen-down, match, verify, revert, canary stop, interrupted → resume/abort), rollout page, audit log. Verified on an R9700 worker: upgrade, launcher refusal → revert, drain behind a running WAN job ([research](../research/rollout-r9700.txt)). Recycle policy moved to later | yes |
| **M5 Heterogeneous acceptance** | CUDA + ROCm farm on the test box, measurements from doc 10, first public release | yes |
| later | HA module, OIDC, Prometheus dashboards, container images, recycle policy, Windows launcher, builder gates (§8) | — |

Publication: the repository goes public at the end of M1 at the earliest (working code + docs), after a secret scan
and a review of every document against the export rules (no internal names, hosts, organisations or credentials).
