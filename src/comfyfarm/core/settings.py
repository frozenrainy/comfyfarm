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
    preview_method: str | None = "auto"    # P16: sent as extra_data.preview_method
    dev_identity: str | None = None        # M1 only: trust X-ComfyFarm-User (loopback deployments)
    dev_admins: tuple[str, ...] = ("admin",)   # M1 only: users treated as admins under dev identity
    default_group: str | None = None       # node definitions shown to users who have not picked a group (docs/design/08)
    gateway_secret: str | None = None      # identity headers are trusted only with this secret (docs/design/09)
    artifacts_dir: str | None = None       # <ART> as the router sees it (optional): artifact lists + §9 checks
