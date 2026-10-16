"""Router process: API + dispatcher + reconcile + health in one asyncio loop (docs/design/01-architecture.md)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .adapter.comfy import ComfyAdapter
from .core import jobs as jobsvc
from .core.dispatcher import Dispatcher
from .core.reconcile import Reconciler
from .core.settings import Settings
from .db import Busy, Database
from .health.monitor import HealthMonitor

log = logging.getLogger(__name__)


class Router:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.dsn)
        self.adapter = ComfyAdapter()
        self.dispatcher = Dispatcher(self.db, self.adapter, settings)
        self.reconciler = Reconciler(self.db, self.adapter, settings, self.dispatcher.wake)
        self.health = HealthMonitor(self.db, self.adapter, settings, self.dispatcher.wake)
        self.tasks: list[asyncio.Task] = []

    async def start(self, loops: bool = True):
        await self.db.start()
        if loops:
            self.tasks = [asyncio.create_task(x.run()) for x in (self.health, self.dispatcher, self.reconciler)]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.adapter.close()
        await self.db.close()


class PromptIn(BaseModel):                 # stock ComfyUI /prompt body + optional ComfyFarm fields
    prompt: dict
    client_id: str | None = None
    prompt_id: str | None = None           # idempotency key chosen by the client (D-23)
    extra_data: dict = {}
    front: bool = False
    comfyfarm_group: int | None = None


def create_app(settings: Settings) -> FastAPI:
    router = Router(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await router.start()
        app.state.router = router
        yield
        await router.stop()

    app = FastAPI(title="ComfyFarm", lifespan=lifespan)

    def identity(user: str | None) -> str:
        # M1: development identity only (loopback). Real identity providers arrive in M2 (docs/design/08).
        if settings.dev_identity is None:
            raise HTTPException(401, "no identity provider configured")
        return user or settings.dev_identity

    @app.exception_handler(Busy)
    async def busy(_req: Request, _exc: Busy):
        return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "2"})

    @app.post("/api/prompt")
    async def post_prompt(body: PromptIn, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        if body.prompt_id is not None:
            try:
                uuid.UUID(body.prompt_id)
            except ValueError:
                raise HTTPException(400, {"error": {"type": "invalid_prompt_id", "message": "prompt_id must be a UUID"},
                                          "node_errors": {}})
        try:
            job, _created = await jobsvc.submit(router.db, owner=owner, prompt=body.prompt, extra_data=body.extra_data,
                                                group_id=body.comfyfarm_group, idempotency_key=body.prompt_id,
                                                browser_client_id=body.client_id, front=body.front)
        except jobsvc.Conflict:
            raise HTTPException(409, "prompt_id conflict")
        router.dispatcher.wake.set()
        return {"prompt_id": str(job["prompt_id"]), "number": job["id"], "node_errors": {}}

    @app.get("/api/farm/jobs/{prompt_id}")
    async def get_job(prompt_id: uuid.UUID, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            r = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1 AND owner = $2", prompt_id, owner)
        if r is None:
            raise HTTPException(404, "not found")
        d = dict(r)
        d["prompt_id"] = str(d["prompt_id"])
        return {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else v) for k, v in d.items()}

    @app.post("/api/farm/jobs/{prompt_id}/cancel")
    async def cancel_job(prompt_id: uuid.UUID, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            jid = await c.fetchval("SELECT id FROM jobs WHERE prompt_id = $1 AND owner = $2", prompt_id, owner)
        if jid is None:
            raise HTTPException(404, "not found")
        return {"status": await jobsvc.request_cancel(router.db, router.adapter, jid)}

    @app.get("/api/health/dispatch")
    async def health_dispatch():
        """Numbers only, per group (docs/design/06). Unknown values are null, never 0."""
        async with router.db.conn() as c:
            rows = await c.fetch("""
              SELECT g.id, g.name,
                (SELECT count(*) FROM jobs j WHERE j.status = 'pending' AND j.group_id = g.id) AS pending,
                (SELECT count(*) FROM workers w WHERE w.group_id = g.id AND w.state = 'online' AND NOT w.maintenance
                   AND w.enabled AND w.deleted_at IS NULL AND w.current_job_id IS NULL) AS idle_workers,
                (SELECT extract(epoch FROM now() - max(j.dispatched_at)) FROM jobs j WHERE j.group_id = g.id) AS since_dispatch,
                (SELECT extract(epoch FROM now() - min(j.created_at)) FROM jobs j WHERE j.status = 'pending' AND j.group_id = g.id) AS oldest_pending,
                (SELECT extract(epoch FROM now() - min(coalesce(j.started_at, j.dispatched_at))) FROM jobs j
                   WHERE j.group_id = g.id AND j.status IN ('running','dispatching')) AS running_longest
              FROM worker_groups g ORDER BY g.id""")
        out = {}
        for r in rows:
            stalled = None
            if r["pending"] and r["idle_workers"]:
                cands = [x for x in (r["since_dispatch"], r["oldest_pending"]) if x is not None]
                stalled = float(min(cands)) if cands else None
            out[str(r["id"])] = {"pending": r["pending"], "idle_workers": r["idle_workers"],
                                 "dispatch_idle_seconds": stalled,
                                 "running_longest_seconds": float(r["running_longest"]) if r["running_longest"] is not None else None}
        return {"groups": out}

    return app
