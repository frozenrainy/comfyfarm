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

Choose a directory every worker can read (a local disk per host, or a shared mount): `<ART>`. Publish:

```sh
git -C ComfyUI archive v0.41.2 | tar -x -C /tmp/core && python3 publish.py core <ART> comfyui-0.41.2 --from /tmp/core
python3 publish.py nodes <ART> my-nodes-1 --from /path/to/a/custom_nodes/dir
python3 publish.py venv  <ART> rocm72-a --uv ~/.local/bin/uv --python 3.12 -r requirements.txt \
    --accelerator rocm --arch gfx1201 --pip-arg=--extra-index-url=https://download.pytorch.org/whl/rocm7.2
```

A venv is built at its final path and links to its base interpreter: every worker using it needs that interpreter at
the same path. Give the router read access to `<ART>` (`artifacts_dir`) and it also checks READY files, platform and
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
`http-agent` backend with `{"url": …, "token": …}`.

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
   workflow is refused, optionally a smoke workflow), and returns to service. If anything fails, that worker goes back
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
