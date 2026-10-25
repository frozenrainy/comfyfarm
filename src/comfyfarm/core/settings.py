from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Settings:
    dsn: str
    instance: str = "comfyfarm-1"          # router instance name; part of the deterministic worker clientId (D-13)
    dispatch_period: float = 1.0           # poll; submits/finishes also wake the dispatcher
    health_period: float = 5.0
    reconcile_period: float = 5.0
    dead_after: float = 15.0               # D-11: connect failures for this long → offline
    busy_after: float = 90.0               # D-11: timeouts for this long → busy_unresponsive
    lost_grace: float = 30.0               # D-06: worker does not know a running job for this long → lost
    absent_settle: float = 5.0             # D-05: "absent" counts only this long after the POST ended
    worker_lost_after: float = 600.0       # D-06b: worker offline this long → its jobs fail
    max_connect_attempts: int = 5          # connect failures before a job fails as unreachable
    retry_gpu_faults: int = 1              # D-44: a job failed by a GPU fault is queued again this often (0: never)
    preview_method: str | None = "auto"    # P16: sent as extra_data.preview_method
    dev_identity: str | None = None        # M1 only: trust X-ComfyFarm-User (loopback deployments)
    dev_admins: tuple[str, ...] = ("admin",)   # M1 only: users treated as admins under dev identity
    default_group: str | None = None       # node definitions shown to users who have not picked a group (docs/design/08)
    gateway_secret: str | None = None      # identity headers are trusted only with this secret (docs/design/09)
    artifacts_dir: str | None = None       # <ART> as the router sees it (optional): artifact lists + §9 checks
    inventory_period: float = 600.0        # re-read a worker's models/nodes this often (models change without a restart)
    keys_dir: str = "/etc/comfyfarm/keys"   # restart backends: ssh keys, known_hosts and agent token files must lie here
    # the compose deployment's builder and UI worker container (docs/design/15); unset = neither
    builds_dir: str | None = None          # requests/ (the router writes) and status/ (the builder writes)
    models_dir: str | None = None          # the farm's model store as the router sees it, read-only (docs/design/17)
    ui_service: str | None = None          # the UI worker container's ComfyUI, e.g. http://ui:8189
    ui_agent: str | None = None            # its agent, e.g. http://ui:8399
    ui_token: str = "ui_agent_token"       # the agent's token, a file in keys_dir
    ui_home: str = "/var/lib/comfyfarm-ui"  # the container's own directories (local copies, models, temp)
    ui_shared: str | None = None           # input/output/user below it: the storage the GPU workers use
    ui_instance: str = "ui-1"
    public_router: str | None = None       # the compose ROUTER_PORT ("8188" or "<ip>:<port>"): the setup form's default
    first_recipe: str = "rocm-gfx1201"     # its first build (the UI half: ComfyUI on the CPU — the same in every recipe)
