# 00 — Overview

> Status: pre-release — milestones M1–M4 implemented and verified on hardware (see [12 roadmap](12-roadmap.md) for
> what each contains and what is left). Every claim about ComfyUI behaviour is either verified by
> `spikes/protocol_probe.py` (see [04](04-worker-protocol.md)) or marked *unverified*.

## What it is

A job router for a farm of [ComfyUI](https://github.com/comfyanonymous/ComfyUI) workers. Users keep using the stock
ComfyUI frontend; the router queues their jobs centrally, hands each job to a suitable idle GPU worker, and relays
progress, live previews and errors back to the browser that submitted the job.

```
 browsers (stock ComfyUI frontend)
        │  one entry point
        ▼
   ┌──────────┐   submit / ws / object_info / jobs   ┌──────────┐   /prompt /history /ws ...   ┌──────────────┐
   │ gateway  │ ───────────────────────────────────▶ │  router  │ ───────────────────────────▶ │ GPU worker 1 │
   │ (proxy)  │ ── user data, static assets ──┐      │          │                              │ GPU worker N │
   └──────────┘                               ▼      └────┬─────┘                              └──────────────┘
                                         UI worker        │ Postgres (ledger)
                                     (CPU-only ComfyUI)
```

## Why it is hard

Dispatching is the easy part. The hard part is that ComfyUI is designed as a **single-user desktop app**, and a farm
breaks its assumptions:

| ComfyUI assumption | What happens in a farm |
|---|---|
| The browser talks to the process that runs the job | Events must be relayed; the browser's WebSocket and the job live on different machines |
| One WebSocket per `clientId` | Reconnecting with the same `clientId` races with the old connection's cleanup and silently drops all further events ([04](04-worker-protocol.md#ws-reconnect-race)) |
| The event loop is mostly free | Loading a large model blocks the loop for minutes; a healthy worker looks dead |
| `/object_info` is cheap | It is synchronous and can take tens of seconds on a worker with many custom nodes |
| Users, queue and history are process-local | They vanish on restart and are invisible to other workers |

Most of the design is a list of decisions made because one of these mismatches caused a real outage in an existing
production farm. Those decisions and their reasons are in [decisions.md](decisions.md).

## Goals

1. **No lost or duplicated work.** Every failure branch of dispatch is classified ([03](03-job-lifecycle.md)); "unknown"
   never silently becomes "failed" or "retry".
2. **Stock ComfyUI, stock frontend.** No fork. Worker-side additions are an optional custom node using official
   extension points; one core fix is proposed upstream.
3. **Honest observability.** Group-level, symptom-level health endpoints whose definitions are documented
   ([06](06-health-and-monitoring.md)).
4. **Safe rolling restarts and version upgrades** that follow the shared upgrade contract `comfy-upgrade/1`
   ([07](07-rollout-and-upgrades.md)).
5. **Mixed hardware.** CUDA and ROCm workers in one farm, routed by group and capability ([10](10-heterogeneous-gpus.md)).
6. **Small-site friendly.** One process + Postgres on a single box is the default; HA is an optional module, designed
   (01, C) and not built yet.

## Non-goals (for now)

- Splitting one job across several GPUs (each job runs on exactly one worker).
- Scheduling by cost/fairness quotas beyond priorities and pause/reorder.
- Managing model files (workers are assumed to already see the models a group needs).
- Being a ComfyUI frontend. We ship a small official-style frontend extension (group picker, retry-safe submit), not a fork.

## Terminology

| Term | Meaning |
|---|---|
| **worker** | One ComfyUI process bound to one GPU (a host with 2 GPUs runs 2 workers) |
| **group** | A set of workers with the same nodes/models. A job targets a group. |
| **job** | One submitted prompt. Has a router-chosen `prompt_id` (UUID) used on the worker as well. |
| **UI worker** | A CPU-only stock ComfyUI that serves the frontend and user data, never runs jobs |
| **gateway** | The thin proxy that is the single entry point for browsers |
| **ledger** | The Postgres database — the source of truth; all in-memory state is a cache of it |
| **lost job** | A job the router believes is running but the worker has never heard of |

## Document map

| Doc | Content |
|---|---|
| [01-architecture](01-architecture.md) | Modules, processes, deployment topologies |
| [02-data-model](02-data-model.md) | Postgres schema and why it is shaped that way |
| [03-job-lifecycle](03-job-lifecycle.md) | State machine, dispatch failure branches, lost jobs, cancel |
| [04-worker-protocol](04-worker-protocol.md) | ComfyUI facts the design depends on, with verification status |
| [05-events](05-events.md) | Progress / preview relay over WebSocket |
| [06-health-and-monitoring](06-health-and-monitoring.md) | busy vs dead, monitoring contract, metric definitions |
| [07-rollout-and-upgrades](07-rollout-and-upgrades.md) | Rolling restart, `comfy-upgrade/1` compliance |
| [08-gateway-and-identity](08-gateway-and-identity.md) | Entry point, identity providers, pre-submit validation |
| [09-security](09-security.md) | Threat model and secure defaults |
| [10-heterogeneous-gpus](10-heterogeneous-gpus.md) | CUDA + ROCm workers |
| [11-testing](11-testing.md) | Fake workers, fault scenarios, negative controls |
| [12-roadmap](12-roadmap.md) | Milestones |
| [decisions](decisions.md) | Decision log with the reason behind each one |
