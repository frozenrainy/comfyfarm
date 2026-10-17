# ComfyFarm

A job router for a farm of [ComfyUI](https://github.com/comfyanonymous/ComfyUI) GPU workers — CUDA and ROCm side by
side. Users keep the stock ComfyUI frontend; ComfyFarm queues their jobs, hands each one to a suitable idle worker, and
relays progress, previews and errors back to the browser that submitted it.

> **Status: pre-release.** Milestones M1–M4a are implemented (core dispatch, events + gateway, monitoring, web UI with
> accounts and group permissions); rolling upgrades (M4b) and the mixed CUDA/ROCm acceptance run (M5) come before a
> first release. See the
> [roadmap](docs/design/12-roadmap.md) for what each milestone contains.

## Why another one?

Dispatching jobs is easy. Running ComfyUI as a farm is not, because ComfyUI is built as a single-user app: its event
loop stops for minutes while it loads a large model, a WebSocket reconnect with the same `clientId` silently drops all
further events, `/object_info` is synchronous, and users, queue and history live inside one process. ComfyFarm's
design is a catalogue of decisions — each tied to a concrete failure seen in production or reproduced against stock
ComfyUI — about how to route around those mismatches without forking ComfyUI or its frontend.

## Highlights

- No lost or duplicated jobs: router-chosen `prompt_id`, classified dispatch outcomes, lost-job detection.
- Stock ComfyUI and stock frontend; an optional worker custom node fixes the WebSocket reconnect race
  ([verified on ComfyUI 0.41.0](docs/design/04-worker-protocol.md#ws-reconnect-race)).
- Busy-vs-dead worker health that tolerates multi-minute model loads.
- Mixed CUDA/ROCm farms with capability-aware routing.
- A web UI (dashboard, queue, history, administration; light/dark) with user accounts and per-user worker-group
  permissions, and `/metrics` for Prometheus with stall numbers defined so that an unknown value never reads as healthy.
- Safe rolling restarts and ComfyUI upgrades via the shared `comfy-upgrade/1` contract (planned, M4).
- Postgres ledger; single box by default, HA as an optional module.

## Documents

Start with [docs/design/00-overview.md](docs/design/00-overview.md) and the [decision log](docs/design/decisions.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
