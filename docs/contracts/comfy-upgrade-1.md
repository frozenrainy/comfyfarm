# comfy-upgrade/1 — ComfyUI farm upgrade contract (export edition)

Status: v1.1, 2026-10-16 (1.1 = additive fields in §9; still `comfy-upgrade/1`). Audience: any farm router implementation that wants to share the same **builder**, **launcher** and **artifact store**.
This is the canonical, export-safe text of the contract. Both implementations version against this document; internal editions only add site notes.

> Principle: **the builder and the launcher never import router code.** A router is compatible if it speaks the HTTP/JSON shapes below.
> All paths in this document are placeholders: `<ART>` artifact root, `<LOCAL>` per-host local root, `<inst>` instance name.

---

## 1. Components and boundaries

```
[builder] --publish--> [artifact store] <--read-- [launcher@worker] --GET render--> [router]
     |                                                 | exec stock ComfyUI
     +-- gate results (state.json, manifest) ---------- read by the router UI
                                       [router] --restart/status--> [restart backend]
```

| Boundary | Shape | § |
|---|---|---|
| Artifact store | directory convention + `manifest.json` + `READY` | 2 |
| Router → launcher | `GET /api/worker_profile?instance=` → render JSON | 3 |
| Launcher behaviour | environment variables, exit codes | 4 |
| State check ("badge") | worker `/system_stats` argv vs render | 5 |
| Router → restart backend | `restart(instance)`, `status(instance)` | 6 |
| Rollout semantics | ordering, revert | 7 |
| Builder | HTTP API, `state.json`, dependency classes A–D | 8 |

Out of scope (implementation-specific): front door / gateway, identity provider, worker access control, notifications.

## 2. Artifacts

```
<ART>/{core,venv,nodes,frontend}/<name>/
    manifest.json   READY   (venv: freeze.txt)
```
- `name`: `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`. A published name is **never reused** with different content.
- `READY` is created only after publish **and** checks. Published artifacts are **read-only** (they may share inodes via hardlinks — never modify in place).
- A venv is **built at its final path** (absolute paths are embedded). It must not reference anything outside itself (`.pth` files, editable installs).
- `core` = the official release archive unpacked as is (no symlinks, no edits).

`manifest.json` — required fields:
```json
{"contract": "comfy-upgrade/1", "kind": "core|venv|nodes|frontend", "name": "...",
 "created": "ISO-8601", "build_host": "...", "files": 0, "bytes": 0, "tree_sha256": "..."}
```
- `tree_sha256`: SHA-256 over the sorted list of (relative path, size) for files and (relative path, target) for symlinks. The launcher uses it to decide whether a local copy is current.
- Optional per kind: core `source_sha256`; venv `base`, `class`, `changed[]`, `sm{}`, `pip_check_inherited[]`, `python`.

## 3. Render JSON — `GET /api/worker_profile?instance=<inst>`

| Response | Meaning | Launcher action |
|---|---|---|
| 200 + render | what this instance should run | validate, then start |
| 403 | caller is not the assigned host | **refuse** (do not use cache) |
| 404 | no assignment | **refuse** |
| anything else / unreachable | router down | start from the last saved render (same `rev`) |

```json
{
 "contract": "comfy-upgrade/1",
 "instance": "<inst>", "profile": "<profile>", "role": "gpu|ui",
 "argv": ["<ART>/venv/<v>/bin/python", "<LOCAL>/core/<c>/main.py",
          "--listen", "<ip>", "--port", "<port>", "--multi-user",
          "--models-directory", "<path>",
          "--extra-model-paths-config", "<LOCAL>/run/<inst>/extra_model_paths.<rev>.yaml",
          "--input-directory", "<path>", "--output-directory", "<path>",
          "--user-directory", "<path>", "--temp-directory", "<path>",
          "...allowed flags"],
 "cwd": "<LOCAL>/core/<c>",
 "env": {"<ALLOWED_KEY>": "<value>"},
 "sync": [{"kind": "core",  "name": "<c>", "src": "<ART>/core/<c>",  "dst": "<LOCAL>/core/<c>"},
          {"kind": "nodes", "name": "<n>", "src": "<ART>/nodes/<n>", "dst": "<LOCAL>/nodes/<n>"}],
 "files": {"<LOCAL>/run/<inst>/extra_model_paths.<rev>.yaml": "<any key>:\n  custom_nodes: <LOCAL>/nodes/<n>\n"},
 "require": ["<ART>/core/<c>/READY", "<ART>/venv/<v>/READY", "<ART>/nodes/<n>/READY"],
 "mkdirs": ["<LOCAL>/run/<inst>", "..."],
 "rev": "<12 hex>"
}
```
- `rev` = first 12 hex of SHA-1 over the render **with `rev` absent and the yaml path still a placeholder**. **Embedding `rev` in the yaml file name inside `argv` is part of the contract** (see §5).
- Models are passed with `--models-directory` (many custom nodes read `folder_paths.models_dir` directly; extra-paths yaml does not change it). Custom nodes are passed via the yaml.
- Optional: `--front-end-root <ART>/frontend/<f>` when a frontend artifact is pinned.

## 4. Launcher
- Configuration (environment): `COMFY_LAUNCH_ROUTER`, `COMFY_LAUNCH_INSTANCE`, `COMFY_LAUNCH_ARTIFACTS`, `COMFY_LAUNCH_LOCAL`, `COMFY_LAUNCH_PREFIXES` (comma-separated allowed path prefixes).
- Validation — all must pass. The launcher is the last gate: a compromised router must not be able to run arbitrary commands.
  - contract major version equal, `instance` equal
  - `argv[0]` is `<ART>/venv/<name>/bin/python`; `argv[1]` is `<LOCAL>/core/<name>/main.py`
  - every following token is a **known option**; path values are under the allowed prefixes; flags and env keys are on allow-lists
  - every `require` READY exists; `sync`, `files` and `mkdirs` stay inside their designated locations
- Exit codes: `--check` passed = 0 / **refused = 2** (one-line reason on stderr) / on success the process `execve`s into ComfyUI (PID preserved for the service manager).
- Local copies: re-synced only when `tree_sha256` differs (temp dir, then rename). Keep the 3 most recent per kind.
- Old `extra_model_paths.*.yaml` files in `<LOCAL>/run/<inst>` are removed at start.

## 5. State check ("badge")
- Read worker `GET /system_stats` → `system.argv` (this is `sys.argv`, **without** the interpreter) and compare with render `argv[1:]`. Equal → `match`, else `differs`.
- 🔴 `sys.argv` does not contain the venv path. Without `rev` in the argv, a venv-only change would show `match` before the restart even happened.
- A change to the render format changes every `rev` → every worker shows `differs`. Router releases that change the format must announce "restart workers".

## 6. Restart backend (chosen by the router)
| Function | Returns | Notes |
|---|---|---|
| `restart(instance)` | (ok, detail) | delivery only; completion is confirmed via §5 |
| `status(instance)` (optional) | `{ActiveState, ExecMainStatus, NRestarts}` | `ExecMainStatus == "2"` = launcher refused → fail the step immediately |

Typical backends: a forced-command SSH agent per host, or a per-instance HTTP agent with a token and an allow-list.

## 7. Rollout semantics
1. Instances are processed **one at a time**. Dispatch targets (GPU workers) are drained first (no new jobs; wait for running jobs).
2. Change assignment → `restart` → wait for `match` (with a time limit) → undo the drain.
3. On failure: **revert that instance only** to its previous profile (restart + confirm `match`); all remaining steps become `skipped`.
4. Every change is audited (who, when, instance, from → to).

## 8. Builder
- Stages: ① read archive → ② publish core → ③ classify dependencies → ④ build & publish venv → ⑤ node gate → ⑥ execution gate → (⑦ canary → ⑧ approve & roll out = router side).
- Dependency classes (from `pip install --dry-run --report` against the base venv, binaries only):
  **A** no change · **B** pure-Python changes (automatic) · **C** binary changes (approval required; attach a CUDA arch report) · **D** unresolvable (source-only, pinned-version conflict, pip ≥ 24.3 with egg installs) → stop.
- `builds/<id>/state.json`:
  ```json
  {"id": "...", "stages": {"1": {"name": "...", "status": "ok|running|awaiting_approval|failed", "reason": "...", "result": {}, "seconds": 0}},
   "result": "gated|awaiting_approval|failed"}
  ```
- HTTP API (header `X-Builder-Token`):
  - `POST /builds?base_venv=&nodes=&samples=` (body = archive) → `{id}`
  - `GET /builds` · `GET /builds/<id>` (state + log tail) · `POST /builds/<id>/approve` · `GET /artifacts?kind=`
  - The router proxies these for authorised users. Creating a "candidate profile" from a gated build (copy an existing profile, replace `core` and `venv`) is a router responsibility.
- Only the builder needs package-index access (a pip-only caching proxy is recommended). Workers and routers can stay offline.

## 9. Additive fields (v1.1 — optional, still `comfy-upgrade/1`)
Proposed by the second implementation (mixed CUDA/ROCm farm with a Windows worker). All fields are optional; consumers that do not know them ignore them, except where a **MUST refuse** rule is stated (that rule is what keeps old consumers safe).

**9.1 Accelerator** — `venv` manifest
```json
{"accelerator": "cuda|rocm|cpu", "arch": ["sm_120", "gfx1201"]}
```
- `arch` lists the device code actually present in the venv's binary packages (CUDA `sm_*`/PTX, ROCm `gfx*`). Absent = unknown.
- Class **C** (§8) reports become an "accelerator arch report" (CUDA: cubin/PTX per package; ROCm: offload targets per package).
- A router that matches artifacts to workers MUST treat an absent `accelerator` as unknown and not auto-assign on it.

**9.2 Platform** — render and `venv` manifest
```json
{"platform": "posix|windows"}
```
- Absent = `posix`. A venv is not portable across platforms: a router MUST NOT assign a venv whose manifest `platform` differs from the worker's.
- **A launcher MUST refuse (exit 2) a render whose `platform` it does not implement.** v1 launchers implement `posix` only.
- `windows` rules (for launchers that implement it):
  - `argv[0]` = `<ART>\venv\<name>\Scripts\python.exe`; `argv[1]` = `<LOCAL>\core\<name>\main.py`; path prefix checks are case-insensitive.
  - There is no `execve` that keeps the PID: the launcher starts ComfyUI as a child, waits, and exits with the child's code. Refusal is still exit 2 before any child starts.
  - Hardlinks (NTFS) and the read-only rule for published artifacts still apply.
- The badge (§5) is unchanged: `rev` in the yaml file name works on both platforms.

---

## Minimum for a compatible router
1. Serve the §3 render (plus §2 naming).
2. Compare per §5 (needed for rollouts).
3. Provide one §6 restart backend.
4. Implement §7 (or add an "assignment change" step to an existing rolling restart).

## Versioning
- Breaking changes bump the major (`comfy-upgrade/2`). The launcher refuses a different major.
- Additive fields are allowed within a major; consumers must ignore unknown fields.
