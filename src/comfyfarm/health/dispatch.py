"""Monitoring contract (docs/design/06 'Monitoring contract'): one computation behind both `/api/health/dispatch` (JSON)
and `/metrics` (Prometheus), so the two cannot disagree. Numbers only, per group; unknown is null, never 0.

The job sets follow the dispatcher (core/dispatcher.py): a worker of group G takes jobs of G and ungrouped jobs, only
those whose requirements it satisfies and whose owner may use G (D-34), and only while it satisfies G's own
requirements.
"""
from __future__ import annotations

# A worker of g that could ever take work: enabled, not deleted, meets the group's requirements (state ignored: an
# offline worker makes a job "waiting", not "unsatisfiable").
_CAPABLE = """w.group_id = g.id AND w.enabled AND w.deleted_at IS NULL AND comfyfarm_satisfies(w.capabilities, g.requirements)"""
# ...and one the dispatcher would hand a job to right now (IDLE_WORKERS + LOCK_WORKER).
_IDLE = _CAPABLE + """ AND w.state = 'online' AND NOT w.maintenance AND w.current_job_id IS NULL"""

SNAPSHOT = f"""
SELECT g.id,
  q.pending, q.unsatisfiable, q.ready, q.oldest_ready, q.paused,
  (SELECT count(*) FROM workers w WHERE {_IDLE}) AS idle_workers,
  (SELECT extract(epoch FROM now() - max(j.dispatched_at)) FROM jobs j JOIN workers w ON w.id = j.worker_id
     WHERE w.group_id = g.id AND j.dispatched_at IS NOT NULL) AS since_dispatch,
  (SELECT extract(epoch FROM now() - min(CASE WHEN j.status = 'dispatching' THEN j.dispatched_at
                                              ELSE coalesce(j.started_at, j.dispatched_at) END))
     FROM jobs j JOIN workers w ON w.id = j.worker_id
     WHERE w.group_id = g.id AND j.status IN ('dispatching','running','cancelling')) AS running_longest,
  (SELECT count(*) FROM jobs j JOIN workers w ON w.id = j.worker_id
     WHERE w.group_id = g.id AND j.status IN ('dispatching','running','cancelling')) AS running,
  (SELECT min(ro.created_at) FROM rollouts ro JOIN rollout_steps st ON st.rollout_id = ro.id JOIN workers w ON w.id = st.worker_id
     WHERE w.group_id = g.id AND ro.status IN ('running', 'aborting', 'interrupted')) AS rollout_started_at
FROM worker_groups g
CROSS JOIN LATERAL (
  SELECT count(*) FILTER (WHERE x.capable) AS pending,
         count(*) FILTER (WHERE NOT x.capable AND j.group_id = g.id) AS unsatisfiable,
         count(*) FILTER (WHERE x.ready) AS ready,
         extract(epoch FROM now() - min(x.waiting_since) FILTER (WHERE x.ready)) AS oldest_ready,
         (SELECT count(*) FROM jobs p WHERE p.status = 'paused' AND p.deleted_at IS NULL AND p.group_id = g.id) AS paused
    FROM jobs j
    CROSS JOIN LATERAL (SELECT
      (j.allowed_groups IS NULL OR g.id = ANY(j.allowed_groups)) AS permitted) p
    CROSS JOIN LATERAL (SELECT
      p.permitted AND EXISTS (SELECT 1 FROM workers w WHERE {_CAPABLE} AND comfyfarm_satisfies(w.capabilities, j.requirements)) AS capable,
      p.permitted AND EXISTS (SELECT 1 FROM workers w WHERE {_IDLE} AND comfyfarm_satisfies(w.capabilities, j.requirements)) AS ready,
      -- back in the queue since (resume, connect failure): created_at alone would make a resumed job look stalled
      coalesce((SELECT max(e.at) FROM job_events e WHERE e.job_id = j.id AND e.to_status = 'pending'), j.created_at)
        AS waiting_since) x
   WHERE j.status = 'pending' AND j.deleted_at IS NULL AND (j.group_id = g.id OR j.group_id IS NULL)
) q
ORDER BY g.id"""

# Ungrouped jobs that no group can ever take: they belong to no group's numbers, so they get their own.
UNGROUPED_UNSATISFIABLE = f"""
SELECT count(*) FROM jobs j
 WHERE j.status = 'pending' AND j.deleted_at IS NULL AND j.group_id IS NULL AND NOT EXISTS (
   SELECT 1 FROM worker_groups g JOIN workers w ON {_CAPABLE} WHERE comfyfarm_satisfies(w.capabilities, j.requirements)
     AND (j.allowed_groups IS NULL OR g.id = ANY(j.allowed_groups)))"""


def _f(v):
    return float(v) if v is not None else None


async def snapshot(conn, iso: bool = True) -> dict:
    """`iso`: timestamps as ISO strings (JSON); False keeps datetimes (Prometheus renders them as unix time)."""
    groups = {}
    for r in await conn.fetch(SNAPSHOT):
        stall = None
        if r["ready"] and r["idle_workers"]:
            # docs/design/06: min(now - last dispatch, oldest dispatchable wait) while an idle worker could take a
            # waiting job. "oldest pending age" alone would measure backlog, not a stall.
            cands = [x for x in (r["since_dispatch"], r["oldest_ready"]) if x is not None]
            stall = float(min(cands)) if cands else None
        groups[str(r["id"])] = {
            "pending": r["pending"], "paused": r["paused"], "running": r["running"],
            "idle_workers": r["idle_workers"],
            "dispatch_idle_seconds": stall,
            "running_longest_seconds": _f(r["running_longest"]),
            "unsatisfiable_pending": r["unsatisfiable"],
            "rollout_started_at": (r["rollout_started_at"].isoformat() if iso else r["rollout_started_at"])
            if r["rollout_started_at"] else None,     # alert rules suppress on this, never on a stuck flag
        }
    return {"groups": groups, "ungrouped_unsatisfiable_pending": await conn.fetchval(UNGROUPED_UNSATISFIABLE)}


# ---- Prometheus text exposition ---------------------------------------------------------------------------------
GAUGES = [  # (field, metric, help)
    ("pending", "comfyfarm_pending_jobs", "Pending jobs a worker of the group can take (same rule as the dispatcher)."),
    ("paused", "comfyfarm_paused_jobs", "Paused jobs of the group."),
    ("running", "comfyfarm_running_jobs", "Jobs dispatching, running or cancelling on workers of the group."),
    ("idle_workers", "comfyfarm_idle_workers", "Online workers without a job, not in maintenance."),
    ("dispatch_idle_seconds", "comfyfarm_dispatch_idle_seconds",
     "Seconds an idle worker could have taken a waiting job; absent when nothing is waiting or nobody is idle."),
    ("running_longest_seconds", "comfyfarm_running_longest_seconds",
     "Age of the oldest job on a worker (running: since start, dispatching: since dispatch); absent when none."),
    ("unsatisfiable_pending", "comfyfarm_unsatisfiable_pending_jobs",
     "Pending jobs of the group that no enabled worker of the group can run (requirements or permissions)."),
    ("rollout_started_at", "comfyfarm_rollout_started_timestamp_seconds",
     "Start of the active rollout (unix time); absent when none."),
]


def prometheus(snap: dict | None) -> str:
    """`snap is None` = the ledger could not be read: only `comfyfarm_ledger_up 0` is exposed, so a scraper sees
    "not observed" instead of a healthy-looking absence of numbers."""
    out = ["# HELP comfyfarm_ledger_up 1 if the router could read the ledger for this scrape.",
           "# TYPE comfyfarm_ledger_up gauge", f"comfyfarm_ledger_up {0 if snap is None else 1}"]
    if snap is None:
        return "\n".join(out) + "\n"
    for field, name, help_ in GAUGES:
        out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        for gid, g in snap["groups"].items():
            v = g[field]
            if v is None:            # unknown stays absent: never 0 (docs/design/06)
                continue
            if hasattr(v, "timestamp"):
                v = v.timestamp()
            out.append(f'{name}{{group="{gid}"}} {float(v) if isinstance(v, float) else v}')
    out += ["# HELP comfyfarm_ungrouped_unsatisfiable_pending_jobs Ungrouped pending jobs no group can take.",
            "# TYPE comfyfarm_ungrouped_unsatisfiable_pending_jobs gauge",
            f"comfyfarm_ungrouped_unsatisfiable_pending_jobs {snap['ungrouped_unsatisfiable_pending']}"]
    return "\n".join(out) + "\n"
