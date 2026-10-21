# 17 — Models from the dashboard: a model store on the control plane, copies on the GPU hosts

Status: **approved** (2026-10-21) — phase 1 in progress, not in a release yet.

## Why

Design 16 took the shared artifact store away; the models are what a GPU host still needs by hand — a share, or files
copied to every worker of a group. On a small NAS that is also the slow part: a 20 GB checkpoint read over 1 GbE takes
about three minutes, from a local NVMe about ten seconds, at every restart or change of model.

How farms get models (operators asked): **from Hugging Face**, many at first, few later — except where every new video
model is added as it comes out (tens of GB each with its text encoders).

## What it is

- **A model store on the control plane** (`COMFYFARM_MODELS`, hard disks are fine), in ComfyUI's folder layout
  (`checkpoints/`, `diffusion_models/`, `text_encoders/`, `vae/`, `loras/` …). The builder owns it, as it owns the
  artifact store.
- **Models are added in the dashboard** (*System → Models*): a Hugging Face file (`repo`, `path`, a revision — pinned to
  its commit, as node packs are), the folder it goes in, and which groups use it. Several at once: paste a list.
- **The builder downloads** (it has internet; the router never does), checks the file against the SHA-256 Hugging Face
  publishes for it, and **signs** an entry (folder, file, size, SHA-256) with the same key as the artifacts (16).
  Gated models: a Hugging Face token the operator puts in a file of the builder's (`COMFYFARM_HF_TOKEN_FILE`) — never in
  the database, never through the router.
- **GPU hosts in copy mode keep local copies** of the models their group uses, in their models directory: the same
  fetch service and account as the artifacts, the same checks (signature, then the file's SHA-256 while it is written).
  A model added to a group is copied to its workers in the background — no restart, no drain: ComfyUI lists a new file
  at its next refresh. A host whose models directory is a share keeps using the share.
- **Downloads resume.** A 30 GB file is fetched in ranges; an interrupted copy (or download from Hugging Face) goes on
  where it stopped.

## Rules

- **No pickles by default.** `.ckpt`, `.pt`, `.pth`, `.bin` can run code when ComfyUI loads them; a router that is taken
  over must not be able to put one on a worker (D-37). Accepted: `.safetensors`, `.sft`, `.gguf`. Another format needs
  the operator's switch in the builder's environment (`COMFYFARM_MODELS_ALLOW_PICKLE=1`), as node pack sources do.
- **Site models stay.** Files the farm did not copy are never touched; the farm's copies are only the ones it lists.
- **Removing** a model from a group removes its copies from that group's workers after their running jobs — the
  dashboard asks first (a workflow that names it then fails on that group).
- **Disk**: the dashboard shows each worker's free space against what its group needs, before copying.
- **Jobs**: a worker reports which of its group's models it has (its inventory, already read for consistency); until a
  copy is complete, the worker counts as "differs from its group" as today.

## Phases

1. Model store, adding from Hugging Face (pinned, checked, signed), groups' model lists, copies on hosts, per-worker
   status in the dashboard, resumable transfers, the pickle rule.
2. Removing models (from groups, from the store), free-space checks before copying, files added by hand to the store
   (a closed network: copy them in, the builder hashes and signs them).

## Not

- Other sources than Hugging Face in phase 1 (Civitai and others: a URL with a known SHA-256 later).
- Converting or quantising models.
- Moving models between workers directly: everything comes from the control plane's store.
