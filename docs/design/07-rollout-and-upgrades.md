# 07 — Rolling restarts and upgrades

ComfyUI upgrades break things: custom nodes, the venv, the frontend. The farm has to change versions one worker at a
time, verify, and stop at the first failure.

## Shared contract: `comfy-upgrade/1`

ComfyFarm implements the **router side** of `comfy-upgrade/1`, a contract shared with another farm router so both can
use the same builder, launcher and artifact store. Whether the contract text itself ships in this repository
(`docs/contracts/comfy-upgrade-1.md`) is pending agreement with its co-owner; until then this page summarises only what
the router must provide:

1. **Render** — `GET /api/worker_profile?instance=` returns the exact `argv`/`env`/`sync`/`files`/`require` for that
   instance with a content hash `rev`; 403 for a caller that is not the assigned host, 404 for no assignment.
2. **Badge** — compare a worker's `/system_stats` `system.argv` with the render's `argv[1:]` → `match | differs`.
   `rev` is embedded in the extra-model-paths yaml file name so a venv-only change is visible (P15: `sys.argv` has no
   interpreter path).
3. **Restart backend** — `restart(instance)` / `status(instance)`; `ExecMainStatus == "2"` means the launcher refused.
4. **Rollout semantics** — one instance at a time, drain → reassign → restart → wait for `match` → undrain; on failure
   revert that instance only and skip the rest; audit every change.

The launcher is the security boundary: it validates the render against allow-lists, so a compromised router cannot run
arbitrary commands on workers.

## Rollout algorithm (router side)

| Step | Detail | Why |
|---|---|---|
| maintenance on | then wait one dispatcher cycle + margin | a claim in flight must not land on a draining worker (the old snapshot race; with `SKIP LOCKED` + re-check under lock the wait is a safety margin, not a correctness requirement) |
| drained | ledger has no job on it **and** worker `/queue` is empty, twice in a row, and once more right before restart | two signals with different lag; a job arriving late must be seen |
| restart | via the configured backend | — |
| observe the restart | the worker must be seen **down** (2 failed probes) within 60 s, else the step fails | a restart call that did nothing must not count as done; PID/uptime are useless (ComfyUI's own restart `execv`s, PID unchanged) |
| wait for up + `match` | time limit | — |
| verify | `/object_info` loads; optional smoke workflow; validation endpoint rejects a deliberately broken workflow with 400 + `node_errors` | `validate_prompt` is internal API; the smoke test catches signature changes |
| undrain | — | — |

- Startup is **serialised** across workers (lock, max wait 15 min): two workers importing the same venv/nodes from shared
  storage at once produced import failures. Draining may overlap.
- Window: up to K workers draining at once; refill a slot with the worker whose running job has the **shortest remaining
  time** (`elapsed × (100 − p) / p`), not by list order; wait 4 s after a worker returns before taking the next one so
  capacity never dips below the window.
- **Canary stop:** the first failed worker stops new steps; steps already running finish; the failed worker stays in
  maintenance (safe default).
- No silent fallback: if the configured restart backend fails, the step fails. (An in-app soft restart re-executes the
  same argv/venv and **cannot change versions** — falling back to it once reported success for an upgrade that never
  happened.)
- After the rollout, refresh the group's `/object_info` snapshot once (D-20). Never invalidate in the middle.
- A rollout whose ledger becomes read-only (HA failover) pauses; if it stays read-only for 60 s the step fails.

## Restart backends (plugins)

| Backend | Use |
|---|---|
| `ssh-forced-command` | per-host SSH key restricted to `restart <instance>` / `status <instance>` |
| `http-agent` | small per-host agent with token + allow-list (Windows-friendly) |
| `systemd-local` | router and worker on the same host |
| `manual` | the dashboard asks an operator to restart and waits for the badge |

## Proposed additive fields for the contract (to discuss with the other implementation)

Our farm mixes CUDA and ROCm workers and includes a Windows worker. Within `comfy-upgrade/1` additive rules:

- `venv` manifest: `accelerator` (`cuda` | `rocm` | `cpu`) and `arch[]` (e.g. `sm_120`, `gfx1201`); class **C** reports
  extended from "CUDA arch report" to "accelerator arch report".
- Render: a Windows layout (`Scripts\python.exe`) is currently outside the contract's `argv[0]` rule — either a
  platform field or a documented limitation.
