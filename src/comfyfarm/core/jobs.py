"""Job state transitions and submission. Only this module changes `jobs.status` (architecture rule 2)."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from ..db import Database
from . import access

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


class NotPermitted(Exception):
    """The owner may not use the job's group, or no group at all (D-34) → 403. The message says which."""


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


TITLE_MAX, HINT_MAX = 100, 160
TEXT_KEYS = ("text", "prompt", "positive_prompt", "positive", "text_g", "string", "value")


def clean_title(s) -> str | None:
    """A title as typed: one line, no control characters, at most TITLE_MAX characters; empty = none."""
    if not isinstance(s, str):
        return None
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in s).split())
    return s[:TITLE_MAX] or None


def job_plan(prompt: dict) -> dict[str, float]:
    """How much of a job's time each node is expected to take, in sampler steps: a sampler counts its `steps`, a VAE
    encode/decode a couple, anything else next to nothing. Node count made a 20-node video workflow sit at 5 % through
    its one KSampler; the browser weighs ComfyUI's per-node progress by this and reads an ETA off the sampler's pace.
    A guess from the API prompt — a second run of the same workflow is still the better clock (design 05)."""
    plan: dict[str, float] = {}
    if not isinstance(prompt, dict):
        return plan
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    for nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        cls = str(node.get("class_type", "")).lower()
        ins = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        steps = ins.get("steps")
        if "sampler" in cls:
            n = float(steps) if num(steps) and steps > 0 else 20.0
            # KSamplerAdvanced runs start_at_step..end_at_step of its schedule (two-pass video: 0–10 of 20)
            start, end = ins.get("start_at_step"), ins.get("end_at_step")
            if num(start) or num(end):
                n = max(1.0, min(n, float(end) if num(end) else n) - (float(start) if num(start) else 0.0))
            plan[str(nid)] = n
        elif "vae" in cls and ("decode" in cls or "encode" in cls):
            plan[str(nid)] = 2.0
        else:
            plan[str(nid)] = 0.1
    return plan


def job_loops(prompt: dict) -> dict[str, dict]:
    """The loops of a workflow: {StartLoop id: {"end": its EndLoop, "body": [ids], "n": iterations or None}}. The body
    is ComfyUI's own (comfy_execution/validation.py): what the start reaches before its end. ComfyUI runs each body
    node once per iteration under a new id, "<start>.<call>.<graph>.<iteration>_<id>", and says "Iteration k / N" as
    each ends; `n` is known here only when the count is typed in (not when another node computes it)."""
    if not isinstance(prompt, dict):
        return {}
    nodes = {str(k): v for k, v in prompt.items() if isinstance(v, dict)}
    children: dict[str, set] = {k: set() for k in nodes}
    for k, n in nodes.items():
        for v in (n.get("inputs") or {}).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in children:
                children[v[0]].add(k)
    ends = {k for k, n in nodes.items() if n.get("class_type") == "EndLoop"}
    loops = {}
    for start, n in nodes.items():
        if n.get("class_type") != "StartLoop":
            continue
        body, reached, todo = set(), set(), list(children[start])
        while todo:                                   # descendants of the start, not walking past an EndLoop
            k = todo.pop()
            if k in body or k in reached:
                continue
            if k in ends:
                reached.add(k)
                continue
            body.add(k)
            todo.extend(children[k])
        if len(reached) != 1:                         # ComfyUI refuses such a prompt; nothing to say about it here
            continue
        ins = n.get("inputs") or {}
        mode, count = ins.get("mode"), None
        num = lambda v: isinstance(v, int) and not isinstance(v, bool)
        if mode == "simple" and num(ins.get("mode.num_iterations")):
            count = ins["mode.num_iterations"]
        elif mode == "For" and all(num(ins.get(f"mode.{x}")) for x in ("start_iteration_index", "max_iteration")):
            step = ins.get("mode.step", 1)
            if num(step) and step != 0:
                count = len(range(ins["mode.start_iteration_index"], ins["mode.max_iteration"], step))
        loops[start] = {"end": next(iter(reached)), "body": sorted(body), "n": count}
    return loops


def workflow_shape(prompt: dict) -> str | None:
    """A workflow's shape, the same for every run of it whatever its seeds, texts or files: node ids and types. What
    the nodes that actually ran last time are remembered under (a lazy input can leave a whole branch out)."""
    if not isinstance(prompt, dict) or not prompt:
        return None
    shape = sorted((str(k), str(v.get("class_type", ""))) for k, v in prompt.items() if isinstance(v, dict))
    return hashlib.sha256(json.dumps(shape, separators=(",", ":")).encode()).hexdigest()[:32]


def job_hint(prompt: dict) -> str | None:
    """What a workflow says about itself, for a person to tell queued jobs apart: where it saves (the first
    filename_prefix) and the start of its prompt — the text the sampler's `positive` leads to, else the first text of
    a prompt node. Read from the API prompt only; nothing of it decides anything."""
    if not isinstance(prompt, dict):
        return None
    nodes = {str(k): v for k, v in prompt.items() if isinstance(v, dict)}
    order = sorted(nodes, key=lambda k: (0, int(k)) if k.isdigit() else (1, k))

    def text_of(nid: str, depth: int = 4) -> str | None:
        n = nodes.get(nid)
        if n is None or depth < 0:
            return None
        ins = n.get("inputs") or {}
        for k in TEXT_KEYS:
            if isinstance(ins.get(k), str) and ins[k].strip():
                return ins[k]
        for k in ("conditioning", "conditioning_1", "positive", "text", "prompt", "string"):       # through combiners
            v = ins.get(k)
            if isinstance(v, list) and v and (got := text_of(str(v[0]), depth - 1)):
                return got
        return None

    prefix = next((nodes[k]["inputs"]["filename_prefix"].strip() for k in order
                   if isinstance((nodes[k].get("inputs") or {}).get("filename_prefix"), str)
                   and nodes[k]["inputs"]["filename_prefix"].strip()), None)
    text = next((got for k in order if isinstance(v := (nodes[k].get("inputs") or {}).get("positive"), list) and v
                 and (got := text_of(str(v[0])))), None)
    if text is None:
        text = next((got for k in order if any(w in str(nodes[k].get("class_type", "")) for w in ("TextEncode", "Prompt"))
                     and (got := text_of(k, 0))), None)
    if text is not None:
        text = " ".join(text.split())
        text = text if len(text) <= 60 else text[:59].rstrip() + "…"
    hint = " · ".join(x for x in (prefix, text) if x)
    return hint[:HINT_MAX] or None


async def submit(db: Database, *, owner: str, prompt: dict, extra_data: dict | None = None, group_id: int | None = None,
                 idempotency_key: str | None = None, browser_client_id: str | None = None, front: bool = False,
                 output_hint: str | None = None, requirements: dict | None = None,
                 allowed_groups: list[int] | None = None, permissions=None, title: str | None = None) -> tuple[dict, bool]:
    """Insert a pending job. Returns (job, created). Unknown group ids degrade to 'no group' (D-22).
    `permissions(conn)` → the owner's groups, read inside the inserting transaction (supersedes `allowed_groups`):
    a submit spends seconds validating, and a revocation in that time must not be missed (D-34)."""
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
            if permissions is not None:
                await access.lock_owner(c, owner)
                allowed_groups = await permissions(c)
                if why := access.refusal(allowed_groups, group_id):
                    raise NotPermitted(why)
            prio = await queue_end(c, front)
            row = await c.fetchrow(
                "INSERT INTO jobs(prompt_id, priority, group_id, owner, idempotency_key, browser_client_id, output_hint, requirements, "
                "allowed_groups, title, hint) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (prompt_id) DO NOTHING RETURNING *",
                uuid.UUID(prompt_id), prio, group_id, owner, uuid.UUID(key) if key else None, browser_client_id, output_hint,
                requirements, allowed_groups, clean_title(title), job_hint(prompt))
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


# ---- queue operations (docs/design/03 'Pause / reorder'): pure ledger operations ---------------------------------
QUEUED = ("pending", "paused")
# advisory lock serialising every priority assignment (submit, retry, move). Its own key: it once shared db.MIGRATION_LOCK's,
# and every Database.start (each router and each CLI command) migrates under that lock — live submits waited behind it.
_PRIORITY_LOCK = 0x636F6D6670   # "comfp"


async def queue_end(c, front: bool) -> int:
    """Priority for a job entering the queue at the front or the back (docs/design/02 'Priority'). Must run inside the
    caller's transaction: the lock makes concurrent submits and a reorder agree on what "the end" is."""
    await c.execute("SELECT pg_advisory_xact_lock($1)", _PRIORITY_LOCK)
    agg = "max(priority) + 1" if front else "min(priority) - 1"
    return await c.fetchval(f"SELECT coalesce({agg}, 0) FROM jobs WHERE status IN ('pending','paused')")


async def pause(db: Database, job_id: int) -> bool:
    async with db.conn() as c:
        async with c.transaction():
            return await transition(c, job_id, "paused", ("pending",), "paused")


async def resume(db: Database, job_id: int) -> bool:
    async with db.conn() as c:
        async with c.transaction():
            ok = await transition(c, job_id, "pending", ("paused",), "resumed")
            if ok:
                await c.execute("NOTIFY comfyfarm_wake")
            return ok


class NotQueued(Exception):
    """Reorder of a job (or relative to a job) that is no longer pending/paused."""


async def move(db: Database, job_id: int, *, to: str | None = None, before: int | None = None,
               after: int | None = None) -> None:
    """Place a queued job at the top/bottom of the queue, or directly before/after another queued job. Order is
    `priority DESC, id` over all queued jobs (one global order; groups only filter it). Relative moves renumber the
    queue, keeping its top value, so a concurrent "front" submit still lands on top."""
    async with db.conn() as c:
        async with c.transaction():
            if to in ("top", "bottom"):
                prio = await queue_end(c, to == "top")
                if await c.fetchval("UPDATE jobs SET priority = $2, updated_at = now() WHERE id = $1 "
                                    "AND status IN ('pending','paused') RETURNING id", job_id, prio) is None:
                    raise NotQueued()
                return
            anchor = before if before is not None else after
            if anchor is None or anchor == job_id:
                raise ValueError("move needs to='top'|'bottom', before or after (another job)")
            await c.execute("SELECT pg_advisory_xact_lock($1)", _PRIORITY_LOCK)
            rows = await c.fetch("SELECT id, priority FROM jobs WHERE status IN ('pending','paused') "
                                 "ORDER BY priority DESC, id FOR UPDATE")
            order = [r["id"] for r in rows]
            if job_id not in order or anchor not in order:
                raise NotQueued()
            order.remove(job_id)
            i = order.index(anchor) + (0 if before is not None else 1)
            order.insert(i, job_id)
            top, old = rows[0]["priority"], {r["id"]: r["priority"] for r in rows}
            changed = [(jid, top - k) for k, jid in enumerate(order) if old[jid] != top - k]
            if changed:
                await c.execute("UPDATE jobs j SET priority = v.p, updated_at = now() FROM unnest($1::bigint[], $2::bigint[]) "
                                "AS v(id, p) WHERE j.id = v.id", [x[0] for x in changed], [x[1] for x in changed])


async def retry(db: Database, job_id: int) -> dict | None:
    """A user retry is a **new job** (new prompt_id) copied from a finished one and appended to the queue
    (docs/design/03). Same owner, group and requirements; NotPermitted if the owner may no longer use them. Comfy.org credentials were never stored (D-29), so API nodes
    in a retried workflow fail visibly instead of running with someone else's key."""
    async with db.conn() as c:
        async with c.transaction():
            old = await c.fetchrow("SELECT j.*, p.prompt, p.extra_data FROM jobs j JOIN job_payloads p ON p.job_id = j.id "
                                   "WHERE j.id = $1 AND j.status = ANY($2::job_status[])", job_id, list(TERMINAL))
            if old is None:
                return None
            await access.lock_owner(c, old["owner"])
            allowed = await access.owner_allowed(c, old["owner"], old["allowed_groups"])   # today's permissions
            if why := access.refusal(allowed, old["group_id"], who="the job's owner"):
                raise NotPermitted(why)                      # as /prompt would: nobody could ever take the copy
            prio = await queue_end(c, False)
            row = await c.fetchrow(
                "INSERT INTO jobs(prompt_id, priority, group_id, owner, output_hint, requirements, retry_of, allowed_groups, "
                "title, hint) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
                uuid.uuid4(), prio, old["group_id"], old["owner"], old["output_hint"], old["requirements"], old["id"], allowed,
                old["title"], old["hint"])
            await c.execute("INSERT INTO job_payloads(job_id, prompt, extra_data) VALUES ($1, $2, $3)",
                            row["id"], old["prompt"], old["extra_data"] or {})
            await c.execute("INSERT INTO job_events(job_id, to_status, reason) VALUES ($1, 'pending', $2)",
                            row["id"], f"retry of job {old['id']}")
            await c.execute("NOTIFY comfyfarm_wake")
    return dict(row)


NOTE_MAX = 500


async def set_note(db: Database, table: str, row_id: int, note: str | None) -> bool:
    assert table in ("jobs", "workers")
    note = (note or "").strip()[:NOTE_MAX] or None
    async with db.conn() as c:
        return await c.fetchval(f"UPDATE {table} SET note = $2 WHERE id = $1 AND deleted_at IS NULL RETURNING id",
                                row_id, note) is not None
