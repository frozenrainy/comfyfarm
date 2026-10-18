# Installing a farm

A ComfyFarm site has four kinds of machines. They can share hosts on a small farm.

```
 users' browsers ──▶ gateway ──▶ router ──▶ GPU workers (ComfyUI + comfyfarm_worker)
                        │          │
                        ▼          ▼
                   UI worker    Postgres
              (ComfyUI, CPU, comfyfarm_ui)
        all ComfyUI instances share one input/output/user storage (NAS)
```

| Part | What it is | Where |
|---|---|---|
| **control plane** | router + gateway + Postgres | one Linux host (containers or systemd) |
| **UI worker** | a stock ComfyUI, CPU is enough, with the `comfyfarm_ui` extension; serves the screen and user files | any host; it must not be busy (no GPU jobs) |
| **GPU workers** | stock ComfyUI with the `comfyfarm_worker` extension, started by the launcher | each GPU machine (≤ ~20 is the design target) |
| **shared storage** | one filesystem for `input`, `output`, `user` on every ComfyUI | a NAS (NFS/SMB) — required; models may be local NVMe |

## 1. Control plane (containers)

On a machine with internet access, build the image and carry it in:

```sh
docker compose -f deploy/compose/docker-compose.yml build
docker save comfyfarm:latest postgres:16 | gzip > comfyfarm-images.tar.gz
```

On the control-plane host inside the network:

```sh
docker load < comfyfarm-images.tar.gz
cd deploy/compose && cp env.example .env
docker compose run --rm --no-deps router comfyfarm secrets >> .env   # then set DB_PASSWORD and UI_WORKER_URL
docker compose up -d
docker compose exec router sh -c 'echo "<password>" | comfyfarm add-user <you> --admin'
```

Users open `http://<control-plane>:8080`. Port 8188 (router) is for the workers' launchers and for monitoring
(`/metrics`); do not publish it to users.

Without containers: `pip install` the package into `/opt/comfyfarm/venv`, put the settings into
`/etc/comfyfarm/{router,gateway}.env` and use `deploy/systemd/comfyfarm-{router,gateway}.service` — see
[running.md](running.md).

## 2. UI worker

A stock ComfyUI on the shared storage, started with `--multi-user --listen <ip>` and the `--input/--output/--user`
directories on the NAS. Copy `ui_ext/comfyfarm_ui` into its `custom_nodes`. It needs no GPU.

## 3. GPU workers

1. Install ComfyUI's GPU stack (driver; on AMD the account needs the `render` and `video` groups for `/dev/kfd`).
2. Copy `launcher.py` and `agent.py` from `src/comfyfarm/` to `~/comfyfarm/` (standard library only).
3. Publish the artifacts once (ComfyUI release, venv, node set incl. `worker_ext/comfyfarm_worker`) — see
   [rollouts.md](rollouts.md).
4. `deploy/systemd/comfyfarm@.service` → `~/.config/systemd/user/`, `worker.env.example` →
   `~/.config/comfyfarm/<instance>.env`, `loginctl enable-linger <account>`, then
   `systemctl --user enable --now comfyfarm@<instance>`.
5. Lock the worker down: put `COMFYFARM_ALLOW=<control-plane address>` in the profile's environment (with containers
   the control plane talks from the host's address). Without it the dashboard shows the worker as **unsecured**.
6. AMD with PyTorch ≥ 2.13 ROCm wheels: also put `HSA_TOOLS_DISABLE_REGISTER=1` into the profile's environment —
   without it every worker burns one CPU core while idle ([rollouts.md](rollouts.md#4-after-a-new-venv-watch-the-idle-worker)).

## 4. In the dashboard

System → Groups (one per set of identical workers) → Workers (add each GPU worker, pick its group) → Rollout
(instance settings, assign the first profile) → Users (accounts, which groups each may use).

## 5. Check

```sh
docker compose exec router comfyfarm doctor --ui http://<ui-worker>:8188
```

Every line should be OK: ledger, an administrator, each worker reachable and *secured*, groups agreeing on nodes and
models, GPU workers seeing files uploaded through the UI worker (shared storage), disk space. Then back up
([backup.md](backup.md)) — before anyone depends on the farm.
