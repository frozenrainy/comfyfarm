# Setting up rolling upgrades

How to let ComfyFarm restart and upgrade workers (design: [07](../design/07-rollout-and-upgrades.md), contract:
[comfy-upgrade/1](../contracts/comfy-upgrade-1.md)). Three small files go to every worker host; they use the Python
standard library only and never import router code.

| File | Role |
|---|---|
| `launcher.py` | started by the service manager: fetches the instance's render from the router, validates it, syncs artifacts, execs ComfyUI |
| `agent.py` | answers the router's `restart <instance>` / `status <instance>` — nothing else |
| `publish.py` | puts ComfyUI releases, node sets and venvs into the artifact store (`READY` last, names never reused) |

## 1. Artifact store

Choose a directory every worker can read at the **same path** (a shared mount, or a copy per host): `<ART>`. Publish
on a machine with internet access that mounts `<ART>` at that path.

**From the dashboard** (the compose deployment's builder, [design 15](../design/15-control-plane-builds.md)): *System →
Rollout → Node packs* lists the farm's custom nodes; *Build* publishes a recipe — the kind of GPU — with them, for the
GPU workers, the UI worker or both, and *From a recipe* turns the result into profiles. Roll the UI profile out first
(it names a pack that fails to import there), then the group's.

**From a recipe** (one command, in a ComfyFarm checkout; git and [uv](https://docs.astral.sh/uv/) on that machine):

```sh
python3 src/comfyfarm/publish.py recipe <ART> rocm-gfx1201
```

`recipes/rocm-gfx1201.json` names a ComfyUI release, the PyTorch index for the GPU and the custom nodes (pinned
commits); the command publishes the release, the GPU and UI node sets (with `comfyfarm_worker`, `comfyfarm_ui`), a
GPU venv and a CPU venv for the UI worker, and writes a profile template to `<ART>/profiles/`. Run again, it reuses
what has the same contents (a venv: the same inputs) and builds only what changed; `--rebuild` builds new venvs.
Then *System → Rollout → Profiles → From a recipe* creates the GPU and UI profiles from that template. With the
compose deployment the router reads the store itself; without it, it reads the store through a GPU worker's host
check — so there, set up one worker first, with *assign later* as its profile.
A host whose setup allowed *builds from the dashboard* (install.md §3, design 14) offers the build there as a button:
while the store has no template, and after a recipe update. A template whose artifacts were retired is shown as such
and not offered. A recipe of your own: copy one in `recipes/` — the ComfyUI `ref` is a release
tag, every node a full commit.

**Custom nodes in a recipe** — one entry per node pack in `nodes`:

```json
{"name": "ComfyUI-VideoHelperSuite", "repo": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
 "commit": "<40 hex>"}
```

What the build does with it, so that the pack works without anyone installing anything on a worker:

- **Requirements come from the pack**: its `requirements.txt`, else the dependencies in its `pyproject.toml`.
  `"requirements_file": "requirements-lite.txt"` picks another file of the pack (several, comma-separated, for a pack that
  keeps one per node); `"requirements": [...]` in the entry
  replaces the pack's own list. Never taken from a pack: options (an index URL, `-r`, `-e`), local paths, and torch,
  torchvision or torchaudio — the venv keeps the torch of its accelerator. The build log says what it left out.
- **One venv, resolved at once**: ComfyUI's requirements, torchaudio (packs import it without listing it) and every
  pack's requirements go into one install, so one pack cannot quietly change what another needed.
- **One OpenCV, one onnxruntime**: packs ask for different OpenCV packages (directly and through their own
  dependencies), and they all install into the same `cv2` — the last one wins. The venv keeps
  `opencv-contrib-python-headless` (every module, none of the desktop libraries a server lacks) and `onnxruntime-gpu`
  on CUDA, `onnxruntime` elsewhere.
- **Git submodules** come along at the commits the pack pins. A pack without them may try to download them when
  ComfyUI imports it, which fails on a farm without internet.
- **Constraints**: `"constraints": ["kornia<0.8"]` in an entry (or for the whole recipe) holds a dependency back where
  a pack breaks on a newer one but does not say so — ComfyUI-LTXVideo needs a kornia before 0.8.
- **The UI worker** gets every pack (their web code, their routes, their node definitions) except those marked
  `"ui": false`: packs that import only with a GPU (the UI worker runs on the CPU). A rollout to the UI worker names a
  pack that fails to import there.

Measured on 42 widely used packs (October 2026, ComfyUI 0.41.0, Python 3.12, CPU venv, no network): with these rules
40 import; the other two did not import without a GPU and are `"ui": false`.

**By hand** — what the recipe does, step by step (`publish.py` needs `launcher.py` next to it):

```sh
git -C ComfyUI archive v0.41.0 | tar -x -C /tmp/core && python3 publish.py core <ART> comfyui-0.41.0 --from /tmp/core
python3 publish.py nodes <ART> farm-nodes-1 --from /path/to/a/dir/holding/comfyfarm_worker   # plus your custom nodes (custom-nodes.md)
uv python install 3.12
python3 publish.py venv <ART> rocm72-a --uv "$(command -v uv)" --python 3.12 -r requirements.txt \
    --accelerator rocm --arch gfx1201 --pip-arg=--index-url=https://download.pytorch.org/whl/rocm7.2 \
    --pip-arg=--extra-index-url=https://pypi.org/simple --pip-arg=--index-strategy=unsafe-best-match
python3 publish.py venv <ART> cpu-a --uv "$(command -v uv)" --python 3.12 -r requirements.txt --accelerator cpu \
    --pip-arg=--index-url=https://download.pytorch.org/whl/cpu --pip-arg=--extra-index-url=https://pypi.org/simple \
    --pip-arg=--index-strategy=unsafe-best-match          # for the UI worker (install.md §2)
```

A venv is built at its final path and carries its interpreter (D-42): any host that mounts `<ART>` at the same path runs
it. GPU wheels still load a few system libraries — the host check names any that a fresh host lacks (ROCm torch:
`libatomic1`). Give the router read access to `<ART>` (`artifacts_dir`) and it also checks READY files, platform and
accelerator before every assignment (contract §9); without it, those checks happen only on the worker.

## 2. Worker host

```ini
# ~/.config/systemd/user/comfyfarm@.service   (user units: `loginctl enable-linger <account>` once)
[Service]
EnvironmentFile=%h/.config/comfyfarm/%i.env
ExecStart=/usr/bin/python3 %h/comfyfarm/launcher.py
Restart=on-failure
RestartPreventExitStatus=2        # 2 = the launcher refused the render: never retry, the router reads it
```

```sh
# ~/.config/comfyfarm/<instance>.env
COMFY_LAUNCH_ROUTER=http://<router>:<port>
COMFY_LAUNCH_INSTANCE=<instance>
COMFY_LAUNCH_ARTIFACTS=<ART>
COMFY_LAUNCH_LOCAL=<LOCAL>                    # local copies of core/nodes + run files
COMFY_LAUNCH_PREFIXES=/data/models,/data/run  # every directory the render may name (models, input, output, user, temp)
```

The directories users write to (input, output, user, temp) must not overlap `<ART>` or `<LOCAL>/core`, `nodes`,
`frontend`, `run` — neither inside them nor above them: ComfyUI imports code from there. Elsewhere under `<LOCAL>`
(`<LOCAL>/temp/<instance>`, say) is fine. The launcher refuses such a render, and the dashboard refuses such instance
paths. Profile environment values are never paths: `COMFYFARM_ALLOW` takes addresses and CIDR blocks, `TZ` a zone
name, every other key a plain value (letters, digits, spaces, `. _ : , ; = + -`, no part starting with `.` or `~`) —
contract §4.

The launcher talks to the router directly, not through the gateway. Check a render without starting anything:
`set -a; . <instance>.env; python3 launcher.py --check` → `ok <rev>` or `refused: <reason>` (exit 2).

Worker access control (design 09): put `COMFYFARM_ALLOW=<router address>,<gateway address>` into the profile's `env`
(or the worker's environment when it is not launched by ComfyFarm). The worker extension then answers 403 to anyone
else on the network; without it the dashboard shows the worker as *unsecured*.

Restart agent, ssh flavour — one key per host, usable for nothing else:

```
# ~/.ssh/authorized_keys of the account running the unit
command="COMFYFARM_AGENT_INSTANCES=<instance> COMFYFARM_AGENT_USER=1 /usr/bin/python3 /home/<account>/comfyfarm/agent.py ssh",no-port-forwarding,no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA… comfyfarm-router
```

HTTP flavour (no ssh, e.g. Windows hosts later): `python3 agent.py serve --port 8399 --token-file <file>` and the
`http-agent` backend with `{"url": "http://<host>:8399", "token_file": "<host>.token"}` (a copy of the token in the
router's keys directory) or `{"url": …, "token_env": "COMFYFARM_AGENT_TOKEN_<HOST>"}` (a variable in the router's
environment). The token value itself is refused: it would be stored in the ledger and its backups.

## 3. In the dashboard (System → Rollout)

1. **Instance** of each worker: instance name, the host address(es) allowed to fetch its render, port, the seven
   paths, restart backend and its configuration (`ssh`: `{"host", "user", "key"}` plus optional `port` and
   `known_hosts`). Key, `known_hosts` and agent token files must lie in the router's keys directory (`keys_dir`,
   environment `COMFYFARM_KEYS_DIR`, default `/etc/comfyfarm/keys`; `/keys` in the compose deployment) — give them
   as a file name there or as a path inside it; anything else is refused.
2. **Profiles**: core, venv, node artifacts, extra flags, environment. A profile in use cannot be changed; copy it.
3. First start: *Assign* the current profile (no restart), start the unit on the host, wait for the badge `match`.
4. Upgrades: *Start rollout* — the worker is drained (its running job finishes), restarted through the backend, must be
   seen going down, must come back with exactly the new argv (badge `match`), is verified (node list, a broken
   workflow is refused, every custom node imported, optionally a smoke workflow), and returns to service. If anything fails, that worker goes back
   to its previous profile and stays in maintenance; the remaining workers are skipped.

The backend `manual` needs no agent: the dashboard tells an operator which instance to restart and waits for it.

## 4. Removing an artifact

Venvs are large; old ones are worth deleting once no profile that a worker might go back to uses them. Do not
delete the directory itself — publish refuses only names that exist, and a name must never be reused. Keep it as a
marker: remove `READY` first (the launcher refuses it from that moment), then everything but `manifest.json`, and
leave a note. `publish.py` makes artifacts read-only, so give the owner write access back first:

```sh
V=<ART>/venv/<name>; chmod u+w "$V" && rm -f "$V/READY" && chmod -R u+w "$V" && \
  find "$V" -mindepth 1 -maxdepth 1 ! -name manifest.json -exec rm -rf {} +
echo "retired $(date +%F): <why>" > "$V/RETIRED"
```

## 5. After a new venv: watch the idle worker

The verification above proves the worker renders; it does not prove it idles. After the first real job on a new venv,
leave the worker idle for a minute and look at its per-thread CPU (`top -H -p <pid>`): an idle ComfyUI uses
nothing. A thread that stays at 100 % costs a core for as long as the worker runs and shows only as a load average of
1.00 on the host.

Known case — AMD, PyTorch ≥ 2.13 ROCm wheels: the bundled `rocprofiler-sdk` registers at start, takes 4096 HSA
signals and exhausts the per-process KFD event pool; the runtime then busy-polls `AMDKFD_IOC_WAIT_EVENTS` forever
(ROCm/rocm-systems#13177, fixed upstream by a smaller pool). Put `HSA_TOOLS_DISABLE_REGISTER=1` into the profile's
environment. The only side effect is that ROCm profilers (`rocprofv3`) cannot attach to that worker.
