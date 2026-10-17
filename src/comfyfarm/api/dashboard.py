"""Pages of the web UI (docs/design/13-web-ui.md): personal dashboard, queue, history, own statistics — and the queue
operations (pause/resume, reorder, retry, notes).

Users see and act on their own jobs; other users' jobs appear only as places in line and in the group numbers.
Admins can list and act on everything. Reordering is admin-only: the queue order is shared, so moving one's own job up
moves everybody else's down (D-32).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..core import jobs as jobsvc
from ..core import stats as statsvc
from ..health.dispatch import snapshot
from .context import Ctx

RECENT_ON_DASHBOARD = 10
QUEUE_LIMIT = 1000
HISTORY_MAX_PAGE = 200
ACTIVE = ("pending", "paused", "dispatching", "running", "cancelling")


class MoveIn(BaseModel):
    to: str | None = None          # "top" | "bottom"
    before: str | None = None      # prompt_id of a queued job
    after: str | None = None


class NoteIn(BaseModel):
    note: str | None = None


def iso(t):
    return t.isoformat() if t else None


# Can any group the owner may use ever run this job? (same rule as the dispatcher, D-03/D-34)
RUNNABLE = """EXISTS (SELECT 1 FROM worker_groups g JOIN workers w ON w.group_id = g.id AND w.enabled AND w.deleted_at IS NULL
                 AND comfyfarm_satisfies(w.capabilities, g.requirements)
               WHERE (j.group_id IS NULL OR g.id = j.group_id)
                 AND (j.allowed_groups IS NULL OR g.id = ANY(j.allowed_groups))
                 AND comfyfarm_satisfies(w.capabilities, j.requirements))"""

JOB_COLS = ("j.id, j.prompt_id, j.status, j.priority, j.group_id, j.owner, j.note, j.progress, j.error, j.created_at, "
            "j.dispatched_at, j.started_at, j.finished_at, j.retry_of, w.name AS worker, g.name AS group_name")
JOB_FROM = "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id LEFT JOIN worker_groups g ON g.id = j.group_id"


def job_doc(r, place=None) -> dict:
    d = {"key": r["id"], "prompt_id": str(r["prompt_id"]), "status": r["status"], "group_id": r["group_id"],
         "group": r["group_name"], "owner": r["owner"], "note": r["note"], "worker": r["worker"],
         "created_at": iso(r["created_at"]), "started_at": iso(r["started_at"] or r["dispatched_at"]),
         "finished_at": iso(r["finished_at"]), "retry_of": r["retry_of"]}
    if r["status"] in ACTIVE:
        d["progress"] = r["progress"]
        d["place"] = place
    else:
        d["run_seconds"] = (r["finished_at"] - r["started_at"]).total_seconds() if r["finished_at"] and r["started_at"] else None
        d["error"] = (r["error"] or {}).get("message") if r["status"] == "failed" else None
    if "runnable" in r.keys():
        d["runnable"] = r["runnable"]
    return d


def search_clause(q: str | None, args: list, admin: bool) -> str:
    """Free text: prompt id prefix, note, error message, and (admins) owner."""
    if not q or not q.strip():
        return ""
    args.append(q.strip().lower())
    n = len(args)
    owner = f" OR lower(j.owner) LIKE '%' || ${n} || '%'" if admin else ""
    return (f" AND (j.prompt_id::text LIKE ${n} || '%' OR lower(coalesce(j.note, '')) LIKE '%' || ${n} || '%'"
            f" OR lower(coalesce(j.error->>'message', '')) LIKE '%' || ${n} || '%'{owner})")


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db = ctx.router, ctx.router.db

    async def job_for(c, prompt_id: str, who: str, admin: bool):
        """The job if `who` owns it or is an admin; None otherwise (no hint that someone else's job exists)."""
        try:
            pid = uuid.UUID(prompt_id)
        except ValueError:
            return None
        r = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1 AND deleted_at IS NULL", pid)
        return r if r is not None and (admin or r["owner"] == who) else None

    async def acting(prompt_id: str, user: str | None):
        who = ctx.identity(user)
        admin = ctx.is_admin(who)
        async with db.conn() as c:
            r = await job_for(c, prompt_id, who, admin)
        if r is None:
            raise HTTPException(404, "not found")
        return who, admin, r

    async def changed(owner: str):
        router.dispatcher.wake.set()
        await router.events.push_status({owner})       # the stock frontend's queue panel follows (D-18)

    def period(days, start, end, tz):
        try:
            return statsvc.period(days, start, end, tz)
        except statsvc.BadRange as e:
            raise HTTPException(400, str(e))

    async def places(c) -> dict[int, int]:
        """Place in line of every queued job within its group (ungrouped jobs share one line)."""
        out, seen = {}, {}
        for r in await c.fetch("SELECT id, group_id FROM jobs WHERE status IN ('pending','paused') AND deleted_at IS NULL "
                               "ORDER BY priority DESC, id"):
            seen[r["group_id"]] = seen.get(r["group_id"], 0) + 1
            out[r["id"]] = seen[r["group_id"]]
        return out

    # ---- dashboard (personal) -----------------------------------------------------------------------------------
    @api.get("/api/farm/dashboard")
    async def dashboard(x_comfyfarm_user: str | None = Header(default=None), days: int = 7, tz: str | None = None):
        who = ctx.identity(x_comfyfarm_user)
        since, until, zone = period(days, None, None, tz)
        async with db.conn() as c:
            allowed = await ctx.allowed(c, who)
            health = await snapshot(c)
            groups = await c.fetch("SELECT g.id, g.name, count(w.id) FILTER (WHERE w.deleted_at IS NULL) AS workers, "
                                   "count(w.id) FILTER (WHERE w.deleted_at IS NULL AND w.enabled AND w.state = 'online') AS online "
                                   "FROM worker_groups g LEFT JOIN workers w ON w.group_id = g.id "
                                   "WHERE $1::bigint[] IS NULL OR g.id = ANY($1) GROUP BY g.id ORDER BY g.name", allowed)
            line = await places(c)
            mine = await c.fetch(f"SELECT {JOB_COLS}, {RUNNABLE} AS runnable {JOB_FROM} WHERE j.owner = $1 AND j.deleted_at IS NULL "
                                 "AND j.status IN ('pending','paused','dispatching','running','cancelling') "
                                 "ORDER BY j.status IN ('pending','paused'), j.priority DESC, j.id", who)
            recent = await c.fetch(f"SELECT {JOB_COLS} {JOB_FROM} WHERE j.owner = $1 AND j.deleted_at IS NULL "
                                   "AND j.status IN ('completed','failed','cancelled') "
                                   f"ORDER BY j.finished_at DESC NULLS LAST, j.id DESC LIMIT {RECENT_ON_DASHBOARD}", who)
            mystats = await statsvc.compute(c, since, until, zone, owner=who)
        return {"groups": [{"id": g["id"], "name": g["name"], "workers": g["workers"], "online": g["online"],
                            "health": health["groups"].get(str(g["id"]))} for g in groups],
                "queue": [job_doc(r, line.get(r["id"])) for r in mine],
                "recent": [job_doc(r) for r in recent],
                "stats": {k: mystats[k] for k in ("since", "until", "total", "groups", "daily")}}

    @api.get("/api/farm/summary")
    async def summary(x_comfyfarm_user: str | None = Header(default=None)):
        """The header's queue badge: the caller's unfinished jobs (polled; cheap)."""
        who = ctx.identity(x_comfyfarm_user)
        async with db.conn() as c:
            r = await c.fetchrow("SELECT count(*) FILTER (WHERE status IN ('pending','paused')) AS queued, "
                                 "count(*) FILTER (WHERE status IN ('dispatching','running','cancelling')) AS running "
                                 "FROM jobs WHERE owner = $1 AND deleted_at IS NULL", who)
        return {"queued": r["queued"], "running": r["running"]}

    @api.get("/api/farm/stats")
    async def my_stats(x_comfyfarm_user: str | None = Header(default=None), days: int | None = None,
                       start: str | None = None, end: str | None = None, tz: str | None = None):
        who = ctx.identity(x_comfyfarm_user)
        since, until, zone = period(days, start, end, tz)
        async with db.conn() as c:
            return await statsvc.compute(c, since, until, zone, owner=who)

    # ---- queue page ---------------------------------------------------------------------------------------------
    @api.get("/api/farm/queue")
    async def queue(x_comfyfarm_user: str | None = Header(default=None), scope: str = "mine", q: str | None = None,
                    status: str | None = None, group: int | None = None):
        who = ctx.identity(x_comfyfarm_user)
        admin = ctx.is_admin(who)
        everyone = admin and scope == "all"
        wanted = [x for x in (status or "").split(",") if x] or list(ACTIVE)
        if bad := set(wanted) - set(ACTIVE):
            raise HTTPException(400, f"unknown status {sorted(bad)}")
        args: list = [wanted]
        where = "j.deleted_at IS NULL AND j.status = ANY($1::job_status[])"
        if not everyone:
            args.append(who); where += f" AND j.owner = ${len(args)}"
        if group is not None:
            args.append(group); where += f" AND j.group_id = ${len(args)}"
        where += search_clause(q, args, everyone)
        async with db.conn() as c:
            line = await places(c)
            rows = await c.fetch(f"SELECT {JOB_COLS}, {RUNNABLE} AS runnable {JOB_FROM} WHERE {where} "
                                 f"ORDER BY j.status IN ('pending','paused'), j.priority DESC, j.id LIMIT {QUEUE_LIMIT}", *args)
            totals = await c.fetchrow("SELECT count(*) FILTER (WHERE status IN ('pending','paused')) AS queued, "
                                      "count(*) FILTER (WHERE status IN ('dispatching','running','cancelling')) AS running "
                                      "FROM jobs WHERE deleted_at IS NULL")
        return {"scope": "all" if everyone else "mine", "jobs": [job_doc(r, line.get(r["id"])) for r in rows],
                "farm": {"queued": totals["queued"], "running": totals["running"]}}

    # ---- history page -------------------------------------------------------------------------------------------
    @api.get("/api/farm/history")
    async def history(x_comfyfarm_user: str | None = Header(default=None), scope: str = "mine", q: str | None = None,
                      status: str | None = None, group: int | None = None, start: str | None = None,
                      end: str | None = None, tz: str | None = None, offset: int = Query(0, ge=0),
                      limit: int = Query(50, ge=1, le=HISTORY_MAX_PAGE)):
        who = ctx.identity(x_comfyfarm_user)
        admin = ctx.is_admin(who)
        everyone = admin and scope == "all"
        terminal = list(jobsvc.TERMINAL)
        wanted = [x for x in (status or "").split(",") if x] or terminal
        if bad := set(wanted) - set(terminal):
            raise HTTPException(400, f"unknown status {sorted(bad)}")
        args: list = [wanted]
        where = "j.deleted_at IS NULL AND j.status = ANY($1::job_status[])"
        if not everyone:
            args.append(who); where += f" AND j.owner = ${len(args)}"
        if group is not None:
            args.append(group); where += f" AND j.group_id = ${len(args)}"
        if start or end:               # calendar dates, any span: this is a search, not a statistic
            try:
                since, until, _ = statsvc.period(None, start, end, tz, max_days=None)
            except statsvc.BadRange as e:
                raise HTTPException(400, str(e))
            args += [since, until]
            where += f" AND j.finished_at >= ${len(args) - 1} AND j.finished_at < ${len(args)}"
        where += search_clause(q, args, everyone)
        async with db.conn() as c:
            total = await c.fetchval(f"SELECT count(*) FROM jobs j WHERE {where}", *args)
            rows = await c.fetch(f"SELECT {JOB_COLS} {JOB_FROM} WHERE {where} "
                                 f"ORDER BY j.finished_at DESC NULLS LAST, j.id DESC OFFSET {offset} LIMIT {limit}", *args)
        return {"scope": "all" if everyone else "mine", "total": total, "offset": offset, "limit": limit,
                "jobs": [job_doc(r) for r in rows]}

    # ---- queue operations ---------------------------------------------------------------------------------------
    @api.post("/api/farm/jobs/{prompt_id}/pause")
    async def pause(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        if not await jobsvc.pause(db, r["id"]):
            raise HTTPException(409, f"only pending jobs can be paused (job is {r['status']})")
        await changed(r["owner"])
        return {"status": "paused"}

    @api.post("/api/farm/jobs/{prompt_id}/resume")
    async def resume(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        if not await jobsvc.resume(db, r["id"]):
            raise HTTPException(409, f"only paused jobs can be resumed (job is {r['status']})")
        await changed(r["owner"])
        return {"status": "pending"}

    @api.post("/api/farm/jobs/{prompt_id}/retry")
    async def retry(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        new = await jobsvc.retry(db, r["id"])
        if new is None:
            raise HTTPException(409, f"only finished jobs can be retried (job is {r['status']})")
        await changed(r["owner"])
        return {"prompt_id": str(new["prompt_id"]), "number": new["id"]}

    @api.post("/api/farm/jobs/{prompt_id}/move")
    async def move(prompt_id: str, body: MoveIn, x_comfyfarm_user: str | None = Header(default=None)):
        _, admin, r = await acting(prompt_id, x_comfyfarm_user)
        if not admin:
            raise HTTPException(403, "admin only: the queue order is shared")
        kw = {}
        if body.to is not None:
            if body.to not in ("top", "bottom"):
                raise HTTPException(400, "to must be 'top' or 'bottom'")
            kw["to"] = body.to
        else:
            ref = body.before if body.before is not None else body.after
            if ref is None:
                raise HTTPException(400, "give to, before or after")
            async with db.conn() as c:
                anchor = await job_for(c, ref, "", True)
            if anchor is None:
                raise HTTPException(404, "anchor job not found")
            kw["before" if body.before is not None else "after"] = anchor["id"]
        try:
            await jobsvc.move(db, r["id"], **kw)
        except jobsvc.NotQueued:
            raise HTTPException(409, "both jobs must be pending or paused")
        except ValueError as e:
            raise HTTPException(400, str(e))
        router.dispatcher.wake.set()
        return {"moved": True}

    @api.put("/api/farm/jobs/{prompt_id}/note")
    async def job_note(prompt_id: str, body: NoteIn, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        await jobsvc.set_note(db, "jobs", r["id"], body.note)
        return {"saved": True}

    return api
