"""Dashboard API (M3): one overview document for polling, and the queue operations (pause/resume, reorder, retry,
notes). Users see and act on their own jobs (other users' jobs show up only as their place in line and in the
group numbers); admins can list and act on everything. Reordering is admin-only: the queue order is shared, so moving
one's own job up moves everybody else's down (D-32).
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core import jobs as jobsvc
from ..health.dispatch import snapshot

RECENT_LIMIT = 50


class MoveIn(BaseModel):
    to: str | None = None          # "top" | "bottom"
    before: str | None = None      # prompt_id of a queued job
    after: str | None = None


class NoteIn(BaseModel):
    note: str | None = None


def iso(t):
    return t.isoformat() if t else None


def gpu_summary(caps: dict) -> dict:
    devs = [d for d in (caps or {}).get("devices") or [] if isinstance(d, dict)]
    vram = max((d.get("vram_total") or 0 for d in devs), default=0)
    return {"backend": (caps or {}).get("backend"), "gpu": devs[0].get("name") if devs else None,
            "vram_gb": round(vram / 2**30, 1) if vram else None, "comfyui": (caps or {}).get("comfyui")}


def make_router(router, identity, is_admin) -> APIRouter:
    api = APIRouter()

    async def job_for(c, prompt_id: str, who: str, admin: bool):
        """The job if `who` owns it or is an admin; None otherwise (no hint that someone else's job exists)."""
        try:
            pid = uuid.UUID(prompt_id)
        except ValueError:
            return None
        r = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1 AND deleted_at IS NULL", pid)
        return r if r is not None and (admin or r["owner"] == who) else None

    async def acting(prompt_id: str, user: str | None):
        who = identity(user)
        admin = is_admin(who)
        async with router.db.conn() as c:
            r = await job_for(c, prompt_id, who, admin)
        if r is None:
            raise HTTPException(404, "not found")
        return who, admin, r

    async def changed(owner: str):
        router.dispatcher.wake.set()
        await router.events.push_status({owner})       # the stock frontend's queue panel follows (D-18)

    # ---- read ---------------------------------------------------------------------------------------------------
    @api.get("/api/farm/overview")
    async def overview(x_comfyfarm_user: str | None = Header(default=None), scope: str = "mine"):
        who = identity(x_comfyfarm_user)
        admin = is_admin(who)
        everyone = admin and scope == "all"
        async with router.db.conn() as c:
            health = await snapshot(c)
            groups = await c.fetch("SELECT g.id, g.name, g.requirements, count(w.id) FILTER (WHERE w.deleted_at IS NULL) AS workers, "
                                   "count(w.id) FILTER (WHERE w.deleted_at IS NULL AND w.enabled AND w.state = 'online') AS online "
                                   "FROM worker_groups g LEFT JOIN workers w ON w.group_id = g.id GROUP BY g.id ORDER BY g.name")
            workers = await c.fetch("SELECT w.*, g.name AS group_name, comfyfarm_satisfies(w.capabilities, g.requirements) AS fits, "
                                    "j.prompt_id AS job_prompt_id, j.owner AS job_owner, j.status AS job_status, j.progress AS job_progress "
                                    "FROM workers w LEFT JOIN worker_groups g ON g.id = w.group_id "
                                    "LEFT JOIN jobs j ON j.id = w.current_job_id WHERE w.deleted_at IS NULL ORDER BY w.name")
            queue = await c.fetch("SELECT j.id, j.prompt_id, j.status, j.priority, j.group_id, j.owner, j.note, j.progress, j.error, "
                                  "j.created_at, j.dispatched_at, j.started_at, j.retry_of, w.name AS worker "
                                  "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id "
                                  "WHERE j.deleted_at IS NULL AND j.status IN ('pending','paused','dispatching','running','cancelling') "
                                  "ORDER BY j.status IN ('pending','paused'), j.priority DESC, j.id")
            recent = await c.fetch("SELECT j.id, j.prompt_id, j.status, j.group_id, j.owner, j.note, j.error, j.created_at, "
                                   "j.started_at, j.finished_at, j.retry_of, w.name AS worker FROM jobs j "
                                   "LEFT JOIN workers w ON w.id = j.worker_id WHERE j.deleted_at IS NULL AND "
                                   "j.status IN ('completed','failed','cancelled') AND ($1 OR j.owner = $2) "
                                   f"ORDER BY j.finished_at DESC NULLS LAST, j.id DESC LIMIT {RECENT_LIMIT}", everyone, who)
            stats = await c.fetch("SELECT coalesce(w.group_id, j.group_id) AS gid, "
                                  "count(*) FILTER (WHERE j.status = 'completed') AS completed, "
                                  "count(*) FILTER (WHERE j.status = 'failed') AS failed, "
                                  "count(*) FILTER (WHERE j.status = 'cancelled') AS cancelled, "
                                  "avg(extract(epoch FROM j.finished_at - j.started_at)) FILTER (WHERE j.status = 'completed') AS avg_run, "
                                  "avg(extract(epoch FROM coalesce(j.dispatched_at, j.started_at) - j.created_at)) "
                                  "  FILTER (WHERE j.status = 'completed') AS avg_wait "
                                  "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id "
                                  "WHERE j.finished_at > now() - interval '24 hours' GROUP BY 1")
        st = {r["gid"]: r for r in stats}
        out_groups = []
        for g in groups:
            s = st.get(g["id"])
            out_groups.append({"id": g["id"], "name": g["name"], "requirements": g["requirements"], "workers": g["workers"],
                               "online": g["online"], "health": health["groups"].get(str(g["id"])),
                               "last_24h": {"completed": s["completed"], "failed": s["failed"], "cancelled": s["cancelled"],
                                            "avg_run_seconds": float(s["avg_run"]) if s["avg_run"] is not None else None,
                                            "avg_wait_seconds": float(s["avg_wait"]) if s["avg_wait"] is not None else None}
                               if s else {"completed": 0, "failed": 0, "cancelled": 0, "avg_run_seconds": None, "avg_wait_seconds": None}})

        streams = router.events.streams
        out_workers = []
        for w in workers:
            warn = []
            if w["group_id"] is None:
                warn.append("no group: receives no jobs")
            elif w["fits"] is False and w["capabilities"]:
                warn.append("does not meet its group's requirements: receives no jobs")
            if w["enabled"] and w["state"] == "online" and w["id"] in streams and not streams[w["id"]][0].connected:
                warn.append("event stream down: no progress or previews")
            d = {"id": w["id"], "name": w["name"], "group": w["group_name"], "state": w["state"],
                 "enabled": w["enabled"], "maintenance": w["maintenance"], "busy": w["current_job_id"] is not None,
                 "failing_since": iso(w["failing_since"]), "last_ok_at": iso(w["last_ok_at"]), "warnings": warn,
                 **gpu_summary(w["capabilities"])}
            mine = w["job_owner"] == who
            if admin or mine:
                d["job"] = {"prompt_id": str(w["job_prompt_id"]), "owner": w["job_owner"], "status": w["job_status"],
                            "progress": w["job_progress"]} if w["job_prompt_id"] else None
            if admin:
                d.update(base_url=w["base_url"], note=w["note"], capabilities=w["capabilities"])
            out_workers.append(d)

        out_queue, place = [], {}
        for r in queue:
            queued = r["status"] in jobsvc.QUEUED
            if queued:                          # place in line within its group (ungrouped jobs share one line)
                place[r["group_id"]] = place.get(r["group_id"], 0) + 1
            if not (everyone or r["owner"] == who):
                continue                        # still counted for everybody else's place in line
            out_queue.append({"key": r["id"], "prompt_id": str(r["prompt_id"]), "status": r["status"],
                              "group_id": r["group_id"], "place": place[r["group_id"]] if queued else None,
                              "owner": r["owner"], "note": r["note"], "worker": r["worker"], "progress": r["progress"],
                              "created_at": iso(r["created_at"]), "started_at": iso(r["started_at"] or r["dispatched_at"]),
                              "retry_of": r["retry_of"]})

        out_recent = [{"key": r["id"], "prompt_id": str(r["prompt_id"]), "status": r["status"], "group_id": r["group_id"],
                       "owner": r["owner"], "note": r["note"], "worker": r["worker"], "retry_of": r["retry_of"],
                       "created_at": iso(r["created_at"]), "finished_at": iso(r["finished_at"]),
                       "run_seconds": (r["finished_at"] - r["started_at"]).total_seconds() if r["finished_at"] and r["started_at"] else None,
                       "error": (r["error"] or {}).get("message") if r["status"] == "failed" else None} for r in recent]
        return {"me": {"user": who, "admin": admin}, "scope": "all" if everyone else "mine", "now": time.time(),
                "groups": out_groups, "workers": out_workers, "queue": out_queue, "recent": out_recent,
                "ungrouped_unsatisfiable_pending": health["ungrouped_unsatisfiable_pending"]}

    # ---- queue operations ---------------------------------------------------------------------------------------
    @api.post("/api/farm/jobs/{prompt_id}/pause")
    async def pause(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        if not await jobsvc.pause(router.db, r["id"]):
            raise HTTPException(409, f"only pending jobs can be paused (job is {r['status']})")
        await changed(r["owner"])
        return {"status": "paused"}

    @api.post("/api/farm/jobs/{prompt_id}/resume")
    async def resume(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        if not await jobsvc.resume(router.db, r["id"]):
            raise HTTPException(409, f"only paused jobs can be resumed (job is {r['status']})")
        await changed(r["owner"])
        return {"status": "pending"}

    @api.post("/api/farm/jobs/{prompt_id}/retry")
    async def retry(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        new = await jobsvc.retry(router.db, r["id"])
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
            async with router.db.conn() as c:
                anchor = await job_for(c, ref, "", True)
            if anchor is None:
                raise HTTPException(404, "anchor job not found")
            kw["before" if body.before is not None else "after"] = anchor["id"]
        try:
            await jobsvc.move(router.db, r["id"], **kw)
        except jobsvc.NotQueued:
            raise HTTPException(409, "both jobs must be pending or paused")
        except ValueError as e:
            raise HTTPException(400, str(e))
        router.dispatcher.wake.set()
        return {"moved": True}

    @api.put("/api/farm/jobs/{prompt_id}/note")
    async def job_note(prompt_id: str, body: NoteIn, x_comfyfarm_user: str | None = Header(default=None)):
        _, _, r = await acting(prompt_id, x_comfyfarm_user)
        await jobsvc.set_note(router.db, "jobs", r["id"], body.note)
        return {"saved": True}

    return api
