# 04 — Worker protocol (what ComfyUI actually does)

The `worker-adapter` module is the only code that talks to ComfyUI. Everything ComfyUI-specific — including its quirks
— is contained there, so a ComfyUI upgrade changes one module. This page lists the behaviours the design depends on.

**Verification column:** `probe` = checked by `spikes/protocol_probe.py` against stock ComfyUI (version noted);
`prod` = observed in production by the operators of an existing farm; `unverified` = assumption to check on the test box.
Raw probe output: [`docs/research/`](../research/).

| # | Fact | Verified | Design consequence |
|---|---|---|---|
| P1 | `POST /prompt` accepts a client-chosen `prompt_id` and uses it as is | probe 0.41.0 (S1) | The router chooses the id before dispatch ([D-04](decisions.md)) |
| P2 | The id must be canonical lowercase hyphenated UUID; otherwise **400 `invalid_prompt_id`** | probe 0.41.0 (S1c) | Generate with `str(uuid4())`; a 400 here is a router bug, alert on it |
| P3 | Re-POSTing an existing `prompt_id` is **accepted (200) and executes again**; history is keyed by id, so the second run overwrites the first | probe 0.41.0 (S1d) | The worker does not deduplicate. Never blind-retry a POST ([D-05](decisions.md)) |
| P4 | `GET /api/jobs` and `GET /api/jobs/{id}` exist; status ∈ `pending, in_progress, completed, failed, cancelled`, with timestamps and output counts | probe 0.41.0 (S2) | Use as the reconciliation source instead of parsing `/history` + `/queue` |
| P5 | Invalid workflow → **400** with `error.type` and per-node `node_errors` | probe 0.41.0 (S4) | 4xx is final, attach `node_errors` to the job ([D-07](decisions.md)) |
| P6 | `/interrupt` with `{"prompt_id": X}` interrupts only if X is running; otherwise logs "not currently running" and does nothing | probe 0.41.0 (S6c) | Targeted cancel is safe to send at any time |
| P7 | Interrupt is checked between nodes / in progress callbacks; a single long node ignored it until it finished (**78–260 s** in our probes, depending on the node's size) | probe 0.41.0 (S6d) | `cancelling` state; the worker stays occupied until it reports ([D-09](decisions.md)) |
| P8 | An interrupted job: `/api/jobs` → `cancelled`; `/history` → `status_str: "error"`, `completed: false`, message `execution_interrupted` with the node | probe 0.41.0 (S6d) | Classify as `cancelled`, not `failed`/`completed` |
| P9 | `POST /queue {"delete": [id]}` removes a pending job; it leaves no history | probe 0.41.0 (S6b, S6e) | Cancel-before-start leaves no trace on the worker; the ledger is the record |
| P10 | WS events of a job go **only** to the socket registered under the job's `client_id`; other sockets receive only `status` broadcasts | probe 0.41.0 (S3b, S3c) | One router socket per worker with a known `client_id`; fake workers must behave the same ([11](11-testing.md)) |
| P11 <a id="ws-reconnect-race"></a> | Reconnecting with the same `clientId`: the old handler's `finally` pops `sockets[sid]` unconditionally and deletes the new registration → the new socket receives **0 job events**, no error anywhere | probe 0.41.0 (S3): 0 events; with the worker extension: 11 events, 3/3 trials | [D-14](decisions.md): worker extension + upstream PR |
| P12 | First WS message `{"type":"feature_flags","data":{...}}` is answered with the server's flags (`supports_preview_metadata`, `max_upload_size`, …) | probe 0.41.0 (S7) | Negotiate preview metadata so binary previews carry the `prompt_id` |
| P13 | `/object_info` is computed synchronously | probe 0.41.0: 0.1 s / 2 MB / 987 nodes on a bare install; prod: 26 s on a heavily customised worker | Snapshot, no TTL ([D-20](decisions.md)) |
| P14 | Loading a large model blocks the event loop: HTTP and WS pings unanswered for minutes (226 s for a 33 GB model to first step) | prod; unverified on our hardware | busy vs dead by elapsed time ([D-11](decisions.md)); WS ping timeout 300 s |
| P15 | `GET /system_stats` → `system.argv` is `sys.argv` **without** the interpreter; `system.comfyui_version`, `system.pytorch_version`, `devices[]` (type, name, vram) | probe 0.41.0 | Version badge for rollouts ([07](07-rollout-and-upgrades.md)); capability detection ([10](10-heterogeneous-gpus.md)) |
| P16 | Live previews are off unless `--preview-method` or `extra_data.preview_method` enables them | prod | Router sets `extra_data.preview_method` per job (configurable) |
| P17 | `/internal/logs/raw` returns the worker's recent log entries with timestamps | observed on 0.38.2 (Windows, CUDA) | Diagnostics without SSH; never needed for control flow |
| P18 | Custom nodes are imported **before** `PromptServer.add_routes()` | source 0.41.0 (`main.py`) | A custom node can replace the `/ws` route definition — basis of the worker extension |

## Worker extension (`comfyfarm_worker`, optional custom node)

Stock ComfyUI works without it, with two known gaps. The extension closes them through official extension points only:

| Feature | Without the extension | With it |
|---|---|---|
| `/ws` reconnect fix (P11) | After a router restart or WS reconnect, a running job's progress may freeze (job still completes) | Fixed. Installs only if the handler source hash matches a known ComfyUI version; otherwise logs a warning and leaves the original |
| Pre-submit validation endpoint | Broken workflows are discovered at dispatch time | `POST /comfyfarm/validate` wraps the internal validator; the router validates on a worker of the target group ([D-24](decisions.md)) |
| Source allow-list | Anyone who can reach the worker can use it (stock `--multi-user` has no auth) | Middleware rejects requests not coming from the router/gateway addresses ([09](09-security.md)) |
| Real latent size / VRAM high-water mark | Unknown (static extraction from the workflow is unreliable) | Reported per job (optional) |

The handler copy is a maintenance cost: every ComfyUI release that changes `websocket_handler` needs a review and a new
hash. Upstreaming the one-line guard removes that cost; the PR is prepared but **not filed** (it is an outward-facing
action and needs the maintainers' approval of this project).

## Open items to verify on the test box (GPU)

- P14 on our own hardware: loop block duration while loading WAN 2.2 14B fp8 (17 GB) on a 16 GB CUDA card and on a 32 GB ROCm card.
- P13 with our custom node set (KJNodes, VHS, DA3, …).
- Preview frames with `supports_preview_metadata` — confirm the binary header layout carries `prompt_id`.
- `/api/jobs` behaviour across a worker restart (does a restart erase it like `/history`? expected yes → lost-job path).
