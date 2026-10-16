"""Job state transitions and submission. Only this module changes `jobs.status` (architecture rule 2)."""
from __future__ import annotations

import logging
import uuid

from ..db import Database

log = logging.getLogger(__name__)

TERMINAL = ("completed", "failed", "cancelled")
ON_WORKER = ("dispatching", "running", "cancelling")


REQUIREMENT_KEYS = {"backend": list, "os": list, "min_vram_gb": (int, float)}


class BadRequirements(ValueError):
    pass


def validate_requirements(req: dict | None) -> dict:
    """Unknown keys are rejected rather than ignored: a silently ignored constraint would run the job somewhere it
    was not meant to run."""
    req = req or {}
    for k, v in req.items():
        if k not in REQUIREMENT_KEYS:
            raise BadRequirements(f"unknown requirement {k!r} (supported: {sorted(REQUIREMENT_KEYS)})")
        if not isinstance(v, REQUIREMENT_KEYS[k]) or isinstance(v, bool):
            raise BadRequirements(f"requirement {k!r} has the wrong type")
    return req


class Conflict(Exception):
    """The idempotency key / prompt_id belongs to another owner's job (D-23) → 409, nothing else revealed."""


async def transition(conn, job_id: int, to: str, frm: tuple[str, ...], reason: str = "", **fields) -> bool:
    """Idempotent conditional transition: only from the given states. Returns True if this call changed the row.
    WS events and reconciliation may race; whoever comes second simply changes nothing."""
    sets = ["status = $2", "updated_at = now()"]
    args: list = [job_id, to]
    if to in TERMINAL:
        sets.append("finished_at = coalesce(finished_at, now())")
    for k, v in fields.items():
        args.append(v)
        if v == "now()":
            args.pop(); sets.append(f"{k} = now()")
        else:
            sets.append(f"{k} = ${len(args)}")
    args.append(list(frm))
    row = await conn.fetchrow(
        f"UPDATE jobs SET {', '.join(sets)} WHERE id = $1 AND status = ANY(${len(args)}::job_status[]) "
        f"RETURNING worker_id, (SELECT status FROM jobs WHERE id = $1) AS old", *args)
    if row is None:
        return False
    await conn.execute("INSERT INTO job_events(job_id, from_status, to_status, reason) VALUES ($1, $2, $3, $4)",
                       job_id, row["old"], to, reason)
    if to in TERMINAL or to == "pending":
        await conn.execute("UPDATE workers SET current_job_id = NULL WHERE current_job_id = $1", job_id)
    return True


async def submit(db: Database, *, owner: str, prompt: dict, extra_data: dict | None = None, group_id: int | None = None,
                 idempotency_key: str | None = None, browser_client_id: str | None = None, front: bool = False,
                 output_hint: str | None = None, requirements: dict | None = None) -> tuple[dict, bool]:
    """Insert a pending job. Returns (job, created). Unknown group ids degrade to 'no group' (D-22)."""
    key = str(uuid.UUID(idempotency_key)) if idempotency_key else None          # canonical lowercase (P2)
    requirements = validate_requirements(requirements)
    prompt_id = key or str(uuid.uuid4())
    async with db.conn() as c:
        async with c.transaction():
            if key:
                existing = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1", uuid.UUID(key))
                if existing:
                    if existing["owner"] != owner:
                        raise Conflict()
                    return dict(existing), False
            if group_id is not None and not await c.fetchval("SELECT 1 FROM worker_groups WHERE id = $1", group_id):
                group_id = None
            agg = "max(priority) + 1" if front else "min(priority) - 1"
            prio = await c.fetchval(f"SELECT coalesce({agg}, 0) FROM jobs WHERE status IN ('pending','paused')")
            row = await c.fetchrow(
                "INSERT INTO jobs(prompt_id, priority, group_id, owner, idempotency_key, browser_client_id, output_hint, requirements) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (prompt_id) DO NOTHING RETURNING *",
                uuid.UUID(prompt_id), prio, group_id, owner, uuid.UUID(key) if key else None, browser_client_id, output_hint,
                requirements)
            if row is None:                                    # concurrent submit with the same key
                existing = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1", uuid.UUID(prompt_id))
                if existing["owner"] != owner:
                    raise Conflict()
                return dict(existing), False
            await c.execute("INSERT INTO job_payloads(job_id, prompt, extra_data) VALUES ($1, $2, $3)",
                            row["id"], prompt, extra_data or {})
            await c.execute("INSERT INTO job_events(job_id, to_status, reason) VALUES ($1, 'pending', 'submitted')", row["id"])
            await c.execute("NOTIFY comfyfarm_wake")
    return dict(row), True


def error_doc(kind: str, message: str, **extra) -> dict:
    return {"kind": kind, "message": message, **extra}


async def request_cancel(db: Database, adapter, job_id: int) -> str:
    """Cancel (docs/design/03 'Cancel'): pending/paused end immediately; on a worker the job becomes `cancelling` and
    the worker is asked to cancel (idempotent, P19). Interrupt is cooperative (P7): reconcile records the real outcome."""
    async with db.conn() as c:
        async with c.transaction():
            if await transition(c, job_id, "cancelled", ("pending", "paused"), "cancelled before dispatch"):
                return "cancelled"
            moved = await transition(c, job_id, "cancelling", ("dispatching", "running"), "cancel requested")
            row = await c.fetchrow("SELECT j.status, j.prompt_id, w.base_url, "
                                   "coalesce((w.capabilities->>'jobs_api')::boolean, true) AS jobs_api "
                                   "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id WHERE j.id = $1", job_id)
    if row is None:
        return "unknown"
    if row["status"] == "cancelling" and row["base_url"]:
        await adapter.cancel(row["base_url"], str(row["prompt_id"]), row["jobs_api"])
    return row["status"] if not moved else "cancelling"
