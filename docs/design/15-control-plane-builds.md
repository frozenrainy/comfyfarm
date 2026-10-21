# 15 — Builds on the control plane, node packs from the dashboard, the UI worker container

Status: **implemented** (2026-10-21, 0.1.0-dev.9) — `builder.py`, `uiworker.py`, `api/builds.py`, the `builder` and `ui`
services of both compose files, the dashboard's *Node packs* and *Build*. Rehearsed on one AMD host: first start →
UI worker running in about a minute → four node packs added and built → UI worker rolled out → a GPU worker set up on
the same host → a job, its output shown through the UI worker.

## Why

Two things were missing after design 14:

- **The router could not put a node pack on the workers.** Custom nodes reached a farm only through recipes — files root
  installs — and builds ran on a GPU worker host whose setup allowed them. A farm without a GPU worker had no way to
  build anything, and an operator had to edit JSON to add a node pack.
- **The UI worker came last.** It was a host set up like a GPU worker, after something had built what it runs. Until
  then the gateway showed no screen.

## The builder

A service of the compose deployment, from the same image, under its own account (uid 10002) that owns the artifact
store. It needs internet (PyPI, the PyTorch indexes, GitHub) or mirrors of them.

```
router (uid 10001) ── writes ──▶ /builds/requests/<id>.json      {"kind": "build", "recipe", "roles", "nodes"}
                                                                 {"kind": "resolve", "repo", "ref"}
builder (uid 10002) ─ writes ──▶ /builds/status/<id>.json, .log  queued → running → done | failed
                                 /builds/status/policy.json      its recipes, its node sources, a heartbeat
```

Each side writes only its own directory. The router never writes the store (it mounts it read-only to list templates
and check artifacts, as before).

**What a router that is taken over cannot do** (D-37 within limits the operator sets): the recipes are the image's own;
node packs from the dashboard are built only from repositories under `COMFYFARM_BUILD_NODE_SOURCES` — the builder's
environment, in the compose file, so the router cannot widen it. Easy sets `https://github.com/`; strict leaves it empty
(no packs from the dashboard) unless the operator names sources. Every pack is pinned to a full commit: the dashboard
asks the builder to resolve a branch or tag (`git ls-remote`), stores the commit, and *Update* moves it on deliberately.
Within the allowed sources a router admin does choose code that runs on the workers — that is what "the router deploys
node packs" means, and why the sources are the operator's switch.

A build runs `publish.py recipe` in a child process with `--roles` (both halves, or one), `--nodes-json` (the
dashboard's packs, checked again by `check_recipe`) and `--platform x86_64-manylinux_2_35` (wheels any glibc ≥ 2.35
host loads: the image's own glibc is newer than many hosts'). One build at a time; a lookup never waits behind a build.
A build running when the builder stops is marked failed on its next start.

## Node packs: what a build does with one

From an analysis of 42 widely used node packs (offline CPU import, ComfyUI 0.41.0): as their authors publish them 37
import; with the rules below 40; the other two need a GPU to import. Built the same way for CUDA and loaded on an
RTX 3080 Ti: 41 — both GPU-only packs among them; the one left lacked system libraries (libGL and eight more for
pymeshlab), which the rollout now names with their packages.

- Requirements come from the pack (`requirements.txt`, else `pyproject.toml`; `requirements_file` picks another).
  Never from a pack: options (an index URL, `-r`, `-e`), local paths, torch/torchvision/torchaudio.
- One install resolves ComfyUI's requirements, `torchaudio` (imported by packs that do not list it) and every pack's —
  installed one after the other, a later file could change what an earlier one needed.
- One OpenCV (`opencv-contrib-python-headless`) and the accelerator's onnxruntime: their variants share one module and
  whichever installs last wins. uv overrides remove the others; a pack's own line becomes the kept package; a variant
  that only a dependency asks for is found by resolving once without the overrides. The dependency check knows what the
  overrides removed.
- Git submodules come along (https only). A pack without them may download them at import, which fails offline.
- `constraints` hold a dependency back where a pack breaks on a newer one without saying so (LTXVideo: `kornia<0.8`).
- The UI worker gets every pack except those marked `"ui": false`; a rollout to the UI worker names one that fails.

Not automated: packs that compile CUDA extensions per GPU generation (sageattention, Hunyuan3D's rasterizer) and models
that packs download at run time — a farm without internet places them itself.

## The UI worker container

The `ui` service runs `comfyfarm ui-worker`: the same launcher as a host — it fetches the render, syncs artifacts, execs
ComfyUI — under a small supervisor that does what the host's unit does (Restart=on-failure, RestartPreventExitStatus=2,
RestartSec=5), and the agent's HTTP side (`restart`, `status`, `check`) with a token `init` makes. It runs as the
workers' uid (47800): the shared storage it serves is theirs too.

- Its port is not published; only the router and the gateway (the `front` network) reach it, and its allow-list adds
  that network (`COMFYFARM_ALLOW_NETWORK`, set by the supervisor) to whatever the profile allows — a profile made for
  GPU workers allows the router's LAN address, which the container never sees.
- Postgres is on a network of its own (`internal`): node pack code in the UI worker cannot reach it.
- Its instance names its host `ui`; the router looks names in `hosts` up when a launcher asks for its render.

**First start**, by the router (`api/builds.py`, every 10 s until done): register the container's UI worker once (an
operator's own UI worker is left alone; one an administrator removed is not added back) → if the store has no template
with a UI half, ask the builder for `COMFYFARM_FIRST_RECIPE`'s UI half → make the profile from that template, assign it,
start the worker. The dashboard's Rollout page shows the state.

## The editor without GPU workers

`/api/object_info` serves a group's snapshot from its GPU workers (D-20). With no GPU worker of the group — or no group
yet — the UI worker's definitions stand in: they are the same node packs on the CPU. They are stored as the group's
snapshot with source `ui:<worker>` and replaced as soon as a GPU worker of the group answers; a real snapshot is never
replaced by them; a rollout to the UI worker refreshes them.

## What the launcher adds (0.1.0-dev.10)

Found on an NVIDIA host (RTX 3080 Ti, 42 packs): onnxruntime-gpu loads cuDNN and the CUDA libraries by name only.
Without the venv's `site-packages/nvidia/*/lib` on `LD_LIBRARY_PATH` its CUDA provider fails and it falls back to the
CPU without an error — every ONNX node runs slowly, silently (cuDNN's directory alone is not enough). The launcher now
puts those directories, the published venv's own, on `LD_LIBRARY_PATH` for ComfyUI; a render still cannot set any
`LD_*` itself. It also starts ComfyUI with `PIP_NO_INDEX=1`: a pack that installs at import time fails at once.

## Found on the NVIDIA host again (0.1.0-dev.11)

- *Set up again* wrote a new environment file, but a rollout of the same profile skipped a worker that already ran
  it, and *Start* refuses a running worker: nothing applied the change. Rollouts take `restart: true` (drained,
  restarted, verified even when nothing in the profile changed); *Update the worker* uses it, and so does *Restart*.
- Right after a GPU worker restarted, a job was validated on the UI worker, which lacks the GPU workers' models, and
  refused. Only GPU workers validate; with none online the job waits and its worker validates it.
- Setup on a host without `ip` died mid-step with a traceback and no report. A missing tool is now a result
  (exit 127), and any unexpected error ends setup as a failed step that is reported like the others.
- A worker reached over a network setup did not guess (its gateway not in the allow-list) refused previews with 403
  and nothing said why. The worker extension (0.7) counts whom it refuses; the dashboard shows the last address.

## Also in this round

- Setup records the address its report came from; when it is not the host's own (NAT, or Docker's bridge on the
  router's own host), it joins the instance's `hosts`, because the launcher's requests come from there too.
- The host check runs each GPU venv's torch against the GPU (a driver older than the wheel's CUDA imports fine and
  then finds no GPU).
- Start on a worker that has a profile and is not running; a running one changes through a rollout.
