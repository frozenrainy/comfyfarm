"""Stock-ComfyUI-shaped endpoints synthesised from the ledger, per user, so the unmodified frontend works through the
gateway (docs/design/08). Shapes follow ComfyUI 0.41.0 (verified against a live worker)."""
from __future__ import annotations

import gzip
import json
import logging
import asyncio
import random
import time
import uuid

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response

from ..core import jobs as jobsvc

log = logging.getLogger(__name__)
UI_SOURCE = "ui:"                     # object_info_snapshots.source of a stand-in from the UI worker

STOCK_STATUS = {"pending": "pending", "paused": "pending", "dispatching": "in_progress", "running": "in_progress",
                "cancelling": "in_progress", "completed": "completed", "failed": "failed", "cancelled": "cancelled"}
LEDGER_FOR_STOCK = {"pending": ["pending", "paused"], "in_progress": ["dispatching", "running", "cancelling"],
                    "completed": ["completed"], "failed": ["failed"], "cancelled": ["cancelled"]}


def ms(t):
    return int(t.timestamp() * 1000) if t else None


def outputs_count(outputs) -> tuple[int, int, dict | None]:
    """(outputs_count, previewable_outputs_count, preview_output) from a stock /history outputs dict."""
    n = p = 0
    first = None
    for node_id, node_out in (outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for kind, items in node_out.items():
            if isinstance(items, list):
                n += len(items)
                if kind in ("images", "gifs", "video", "videos", "animated"):
                    p += len(items)
                    if first is None and items and isinstance(items[0], dict):
                        # as ComfyUI's own jobs API: the frontend's queue panel needs both, or drops the whole list
                        first = {**items[0], "nodeId": str(node_id), "mediaType": items[0].get("mediaType", kind)}
    return n, p, first


def outputs_files(outputs) -> list[dict]:
    """Every file a job saved, in node order, from a stock /history outputs dict: what the dashboard shows behind
    "+N" next to the first one. Entries without a file name (text outputs, numbers) are not files."""
    files = []
    for node_id, node_out in (outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for kind, items in node_out.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("filename"):
                    files.append({"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": it.get("type", "output"),
                                  "mediaType": it.get("mediaType", kind), "node": str(node_id)})
    return files


def make_router(ctx) -> APIRouter:
    api = APIRouter()
    router, identity = ctx.router, ctx.identity

    def job_item(r) -> dict:
        n, p, first = outputs_count(r["outputs"] if "outputs" in r else None)
        d = {"id": str(r["prompt_id"]), "status": STOCK_STATUS[r["status"]], "priority": r["priority"],
             "create_time": ms(r["created_at"]), "execution_start_time": ms(r["started_at"] or r["dispatched_at"]),
             "execution_end_time": ms(r["finished_at"]), "outputs_count": n, "previewable_outputs_count": p}
        if first:
            d["preview_output"] = first
        return d

    def exec_status(r) -> dict:
        st = r["status"]
        msgs = []
        if st == "failed" and r["error"]:
            msgs.append(["execution_error", {"prompt_id": str(r["prompt_id"]), "exception_message": r["error"].get("message", ""),
                                             "node_id": r["error"].get("node_id"), "node_type": r["error"].get("node_type")}])
        if st == "cancelled":
            msgs.append(["execution_interrupted", {"prompt_id": str(r["prompt_id"])}])
        return {"status_str": "success" if st == "completed" else "error", "completed": st == "completed", "messages": msgs}

    @api.get("/api/jobs")
    async def list_jobs(x_comfyfarm_user: str | None = Header(default=None), status: str | None = None,
                        sort_by: str = "created_at", sort_order: str = "desc", limit: int | None = Query(None, gt=0),
                        offset: int = Query(0, ge=0)):
        owner = identity(x_comfyfarm_user)
        ledger: list[str] = []
        if status:
            for s in [x.strip().lower() for x in status.split(",") if x.strip()]:
                if s not in LEDGER_FOR_STOCK:
                    raise HTTPException(400, {"error": f"Invalid status value(s): {s}"})
                ledger += LEDGER_FOR_STOCK[s]
        order = "coalesce(finished_at, now()) - coalesce(started_at, dispatched_at, created_at)" if sort_by == "execution_duration" else "created_at"
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        async with router.db.conn() as c:
            where = "owner = $1 AND deleted_at IS NULL" + (" AND status = ANY($2::job_status[])" if ledger else "")
            args = [owner] + ([ledger] if ledger else [])
            total = await c.fetchval(f"SELECT count(*) FROM jobs WHERE {where}", *args)
            rows = await c.fetch(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} {direction}, id {direction} "
                                 f"OFFSET {int(offset)} " + (f"LIMIT {int(limit)}" if limit else ""), *args)
        lim = limit if limit else total
        return {"jobs": [job_item(r) for r in rows],
                "pagination": {"offset": offset, "limit": lim, "total": total, "has_more": offset + len(rows) < total}}

    async def own_job(c, prompt_id: str, owner: str):
        try:
            pid = uuid.UUID(prompt_id)
        except ValueError:
            return None
        return await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1 AND owner = $2 AND deleted_at IS NULL", pid, owner)

    @api.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            r = await own_job(c, job_id, owner)
            if r is None:
                raise HTTPException(404, {"error": "Job not found"})
            p = await c.fetchrow("SELECT prompt, extra_data FROM job_payloads WHERE job_id = $1", r["id"])
        d = job_item(r)
        d.update({"outputs": r["outputs"] or {}, "execution_status": exec_status(r),
                  "workflow": {"prompt": p["prompt"] if p else {}, "extra_data": p["extra_data"] if p else {}}})
        return d

    async def queue_items(owner: str, ledger: list[str]):
        async with router.db.conn() as c:
            rows = await c.fetch("SELECT j.id, j.prompt_id, p.prompt, p.extra_data FROM jobs j JOIN job_payloads p ON p.job_id = j.id "
                                 "WHERE j.owner = $1 AND j.deleted_at IS NULL AND j.status = ANY($2::job_status[]) "
                                 "ORDER BY j.priority DESC, j.id", owner, ledger)
        return [[r["id"], str(r["prompt_id"]), r["prompt"], r["extra_data"] or {},
                 [k for k, n in (r["prompt"] or {}).items() if isinstance(n, dict) and n.get("class_type", "").startswith(("Save", "Preview"))]]
                for r in rows]

    @api.get("/api/queue")
    async def get_queue(x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        return {"queue_running": await queue_items(owner, LEDGER_FOR_STOCK["in_progress"]),
                "queue_pending": await queue_items(owner, LEDGER_FOR_STOCK["pending"])}

    @api.post("/api/queue")
    async def post_queue(request: Request, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        body = await request.json()
        async with router.db.conn() as c:
            if body.get("clear"):
                ids = [r["id"] for r in await c.fetch("SELECT id FROM jobs WHERE owner = $1 AND status IN ('pending','paused')", owner)]
            else:
                ids = []
                for pid in body.get("delete", []):
                    r = await own_job(c, pid, owner)
                    if r is not None:
                        ids.append(r["id"])
        for jid in ids:
            await jobsvc.request_cancel(router.db, router.adapter, jid)
        return Response(status_code=200)

    @api.post("/api/interrupt")
    async def interrupt(request: Request, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        async with router.db.conn() as c:
            if body.get("prompt_id"):
                r = await own_job(c, body["prompt_id"], owner)
                ids = [r["id"]] if r is not None and r["status"] in ("dispatching", "running") else []
            else:      # stock semantics: interrupt "the running prompt" → this user's running jobs
                ids = [x["id"] for x in await c.fetch("SELECT id FROM jobs WHERE owner = $1 AND status IN ('dispatching','running')", owner)]
        for jid in ids:
            await jobsvc.request_cancel(router.db, router.adapter, jid)
        return Response(status_code=200)

    @api.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            r = await own_job(c, job_id, owner)
        if r is None or r["status"] in jobsvc.TERMINAL:
            return {"cancelled": False}
        await jobsvc.request_cancel(router.db, router.adapter, r["id"])
        return {"cancelled": True}

    def history_item(r, prompt) -> dict:
        return {"prompt": [r["id"], str(r["prompt_id"]), prompt or {}, {}, []], "outputs": r["outputs"] or {},
                "status": exec_status(r)}

    @api.get("/api/history")
    async def history(x_comfyfarm_user: str | None = Header(default=None), max_items: int | None = None):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            rows = await c.fetch("SELECT * FROM jobs WHERE owner = $1 AND deleted_at IS NULL AND status IN ('completed','failed','cancelled') "
                                 "ORDER BY finished_at DESC " + (f"LIMIT {int(max_items)}" if max_items else "LIMIT 200"), owner)
        return {str(r["prompt_id"]): history_item(r, None) for r in rows}

    @api.get("/api/history/{prompt_id}")
    async def history_one(prompt_id: str, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            r = await own_job(c, prompt_id, owner)
            if r is None or r["status"] not in jobsvc.TERMINAL:
                return {}
            p = await c.fetchval("SELECT prompt FROM job_payloads WHERE job_id = $1", r["id"])
        return {prompt_id: history_item(r, p)}

    @api.post("/api/history")
    async def post_history(request: Request, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        body = await request.json()
        async with router.db.conn() as c:
            if body.get("clear"):
                await c.execute("UPDATE jobs SET deleted_at = now() WHERE owner = $1 AND status IN ('completed','failed','cancelled')", owner)
            for pid in body.get("delete", []):
                try:
                    await c.execute("UPDATE jobs SET deleted_at = now() WHERE prompt_id = $1 AND owner = $2 AND "
                                    "status IN ('completed','failed','cancelled')", uuid.UUID(pid), owner)
                except ValueError:
                    pass
        return Response(status_code=200)

    # ---- node definitions (D-20) -------------------------------------------------------------------------------
    async def group_for(c, who: str, header_group: str | None):
        """The group whose node definitions this user sees: the one picked in the frontend, else the site default,
        else the first — always among the groups the user may use (D-34)."""
        allowed = await ctx.allowed(c, who)
        # The extension sends the group's id: header values must be Latin-1, so a name like "영상팀" broke every
        # request. A plain (ASCII) name is still accepted from older extensions; unknown ids and names fall through.
        tries = []
        if header_group and header_group.isascii() and header_group.isdigit() and len(header_group) <= 18:
            tries.append(("id", int(header_group)))
        tries += [("name", header_group), ("name", router.s.default_group)]
        for col, value in tries:
            if value:
                g = await c.fetchrow(f"SELECT id, name FROM worker_groups WHERE {col} = $1 AND "
                                     "($2::bigint[] IS NULL OR id = ANY($2))", value, allowed)
                if g:
                    return g
        return await c.fetchrow("SELECT id, name FROM worker_groups WHERE $1::bigint[] IS NULL OR id = ANY($1) "
                                "ORDER BY id LIMIT 1", allowed)

    @api.get("/api/object_info")
    async def object_info(x_comfyfarm_user: str | None = Header(default=None), x_comfyfarm_group: str | None = Header(default=None)):
        who = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            g = await group_for(c, who, x_comfyfarm_group)
            snap = g and await c.fetchrow("SELECT data, source, stale FROM object_info_snapshots WHERE group_id = $1", g["id"])
            # definitions taken from the UI worker stand in until a GPU worker of the group can give its own
            stand_in = bool(snap) and (snap["source"] or "").startswith(UI_SOURCE)
            gpu_up = bool(g) and await c.fetchval("SELECT EXISTS (SELECT 1 FROM workers WHERE group_id = $1 AND enabled AND "
                                                  "deleted_at IS NULL AND NOT maintenance AND state = 'online')", g["id"])
        if g is None:                       # a new farm without groups: the screen still opens, with the UI worker's nodes
            data = await ui_object_info()
            if data is None:
                raise HTTPException(503, "no worker group available to you, and no UI worker answered")
            return Response(data[1], media_type="application/json")
        # out of date (its definitions changed and could not be read then): read again once a worker answers — at
        # most once a minute, so a worker whose /object_info hangs does not hold up every editor that opens
        due = bool(snap) and snap["stale"] and gpu_up and time.monotonic() - stale_tried.get(g["id"], -1e9) > 60
        if due:
            stale_tried[g["id"]] = time.monotonic()
        if snap is None or (stand_in and gpu_up) or due:
            snap_data = await refresh_once(g["id"])
            if snap_data is not None:
                return Response(snap_data, media_type="application/json")
            if snap is None:
                raise HTTPException(503, "no node definitions yet: no worker of this group or UI worker answered")
        return Response(gzip.decompress(snap["data"]), media_type="application/json")

    @api.get("/api/object_info/{node_class}")
    async def object_info_one(node_class: str, x_comfyfarm_user: str | None = Header(default=None),
                              x_comfyfarm_group: str | None = Header(default=None)):
        r = await object_info(x_comfyfarm_user, x_comfyfarm_group)
        data = json.loads(r.body)
        return {node_class: data[node_class]} if node_class in data else {}

    async def fetch_object_info(w) -> tuple[bytes, int] | None:
        try:
            # multi-MB, slow to make: 90 s to read — but a worker that is away is known in seconds (a read after a copy
            # or a rollout asks one the health monitor may not have seen go)
            r = await router.adapter.http.get(f"{w['base_url']}/object_info", timeout=httpx.Timeout(90, connect=5))
            if r.status_code != 200:
                return None
            return r.content, len(r.json())
        except Exception as e:
            log.warning("object_info from %s failed: %s", w["name"], type(e).__name__)
            return None

    async def ui_object_info(prefer: list[int] | None = None) -> tuple[str, bytes, int] | None:
        """The UI worker's own definitions. It runs the same node sets as the GPU workers (their CPU side), so a farm
        whose GPU workers are not there yet still opens workflows and shows its node packs. A pack that imports only
        with a GPU is missing from them until a GPU worker answers. `prefer`: asked even before the health monitor
        sees them online (a rollout just saw them start)."""
        async with router.db.conn() as c:
            ws = await c.fetch("SELECT name, base_url FROM workers WHERE kind = 'ui' AND enabled AND deleted_at IS NULL "
                               "AND (state = 'online' OR id = ANY($1::bigint[])) ORDER BY id", list(prefer or []))
        for w in ws:
            got = await fetch_object_info(w)
            if got:
                return w["name"], got[0], got[1]
        return None

    async def refresh_snapshot(group_id: int, prefer: list[int] | None = None) -> bytes | None:
        """Fetch /object_info from an online worker of the group (idle first, up to 3); with none, from the UI worker,
        kept as a stand-in until a GPU worker of the group answers. `prefer`red workers come first and are asked even
        before the health monitor sees them online: a rollout that just saw them start the new profile (field, dev.17:
        it ended 5 s before the monitor saw the worker back, nobody was asked, and the editor kept the definitions from
        before two node packs), or those that have a model the others are still copying. On failure the existing
        snapshot stays — a 502 here would leave the frontend with an empty canvas (D-20) — marked stale: read again
        on the editor's next request once a worker answers."""
        prefer = list(prefer or [])
        async with router.db.conn() as c:
            ws = await c.fetch("SELECT id, name, base_url, current_job_id IS NULL AS idle FROM workers WHERE group_id = $1 "
                               "AND enabled AND deleted_at IS NULL AND NOT maintenance "
                               "AND (state = 'online' OR id = ANY($2::bigint[]))", group_id, prefer)
            had = await c.fetchval("SELECT source FROM object_info_snapshots WHERE group_id = $1", group_id)
        idle = [w for w in ws if w["idle"]]; busy = [w for w in ws if not w["idle"]]
        random.shuffle(idle); random.shuffle(busy)
        order = idle + busy
        if prefer:
            order = [w for w in order if w["id"] in prefer] + [w for w in order if w["id"] not in prefer]
        got = None
        for w in order[:3]:
            if got := await fetch_object_info(w):
                source = w["name"]
                break
        # the UI worker only stands in where nothing better was ever had: GPU workers that are there but failed, or
        # a real snapshot whose workers are away for now, keep what the group had (stale-if-error)
        if got is None and not ws and (had is None or had.startswith(UI_SOURCE)):
            if ui := await ui_object_info(prefer):
                source, got = UI_SOURCE + ui[0], ui[1:]
        if got is None:
            if had is not None:
                async with router.db.conn() as c:
                    await c.execute("UPDATE object_info_snapshots SET stale = true WHERE group_id = $1", group_id)
            return None
        data, n = got
        async with router.db.conn() as c:
            await c.execute("INSERT INTO object_info_snapshots(group_id, data, node_count, source, fetched_at) "
                            "VALUES ($1, $2, $3, $4, now()) ON CONFLICT (group_id) DO UPDATE SET data = excluded.data, "
                            "node_count = excluded.node_count, source = excluded.source, fetched_at = now(), stale = false",
                            group_id, gzip.compress(data), n, source)
        return data

    stale_tried: dict[int, float] = {}
    inflight: dict[int, asyncio.Future] = {}

    async def refresh_once(group_id: int) -> bytes | None:
        """One read per group at a time: editors opening together wait for the same one."""
        f = inflight.get(group_id)
        if f is None:
            f = inflight[group_id] = asyncio.ensure_future(refresh_snapshot(group_id))
            f.add_done_callback(lambda _f: inflight.pop(group_id, None))
        return await asyncio.shield(f)

    router.refresh_snapshot = refresh_snapshot
    return api
