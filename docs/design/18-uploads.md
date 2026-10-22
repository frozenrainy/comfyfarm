# 18 — Uploads: models and node packs from the operator's own files

Status: **agreed** (2026-10-22) — implemented (0.1.0-dev.18).

## Why

Design 17 brings models from Hugging Face, and design 15 brings node packs from git. A farm also has its own:
fine-tuned LoRAs, models from elsewhere, and node packs that live in no repository (a studio's own tools, a pack
handed over as a zip). Until now such files went by hand onto every worker.

## What it is

### Models

- **Uploaded from the dashboard** (*System → Models → Upload*). Several files at once. The folder is picked for the
  batch, and each file can have its own. The groups are picked as for Hugging Face models.
- **In chunks, resumable**: a file goes up in 16 MiB pieces. An interrupted upload goes on from the last piece the
  router has; after a reload the dashboard offers it again (pick the same file).
- **The inbox**, for files too large for a browser or a control plane without internet nearby:
  `<model store>/inbox/<folder>/` on the control-plane host. Files copied there are registered by *Scan the inbox*.
- **The builder takes them in** — never the router. It checks the file name and the format rule (no pickles without
  `COMFYFARM_MODELS_ALLOW_PICKLE=1`, as in 17), hashes the file, puts it in place and signs its entry. From there on an
  uploaded model is like any other: its groups' workers get copies, checked against the signature.
  - An upload lands in `<model store>/.uploads/` (the router's, on the store's disk). The builder copies it into
    place while hashing it: the file in the store is the builder's, and the router cannot change it after it was
    signed. The router then removes the upload.
  - A file from the inbox is moved, not copied (it is the operator's, put there by hand): instant on any size. It
    must be readable by everyone (mode 644).

### Node packs

- **An archive** (`.zip`, `.tar.gz`, `.tgz`, `.tar`) uploaded in *System → Rollout → Node packs → Upload*, in chunks
  like models (at most 2 GiB).
- **Pinned by its SHA-256** as a git pack is pinned by its commit. *Update* is another upload; the pack keeps its
  name, UI-worker setting, requirements file and constraints.
- **The builder unpacks it** when it builds: a single top-level directory is taken as the pack (as GitHub's
  "Download ZIP" makes it). It unpacks regular files and directories only. Absolute paths, `..`, links and devices
  are refused, `.git` is left out, and an archive that unpacks to more than 4 GiB or 100 000 entries is refused. The
  rest — requirements, constraints, the UI worker — is as for git packs.
- **The builder's switch**: `COMFYFARM_BUILD_NODE_ARCHIVES=1`. An archive does not come from the allowed sources of
  `COMFYFARM_BUILD_NODE_SOURCES`, so a router that someone took over could hand the builder any code. The operator
  turns it on in the builder's environment, which the dashboard cannot change.
  - Easy deployment: on by default. It already allows all of GitHub by default, which is the same exposure.
  - Strict deployment: off by default.

## Rules

- Uploads are administrators' only, as the rest of *System*.
- An upload that is not finished within 24 hours is dropped, with its part file.
- The router stores uploads only in two places: `<model store>/.uploads/` and `<builds>/archives/`. It writes nowhere
  else in the store.
- Names: a model file name follows the store's rule (letters, digits, space and `._+()-`); an existing
  `<folder>/<file>` is refused, as for Hugging Face models.

## Not

- Uploading other artifacts (venvs, ComfyUI itself): those are built.
- Removing models (17, phase 2) and old archives: they stay until removing comes.
