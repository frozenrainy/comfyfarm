"""Worker health: busy vs dead by elapsed time (D-11), probes in parallel, results applied serially (D-12)."""
from __future__ import annotations

import asyncio
import logging

from ..adapter.comfy import ComfyAdapter
from ..db import Busy, Database
from ..core.settings import Settings

log = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, db: Database, adapter: ComfyAdapter, settings: Settings, wake: asyncio.Event | None = None):
        self.db, self.adapter, self.s, self.wake = db, adapter, settings, wake

    async def run(self):
        while True:
            try:
                await self.tick()
            except Busy:
                log.warning("health: database busy")
            except Exception:
                log.exception("health tick failed")
            await asyncio.sleep(self.s.health_period)

    async def tick(self):
        async with self.db.conn() as c:
            workers = [dict(r) for r in await c.fetch(
                "SELECT id, base_url, state, capabilities, failing_since FROM workers WHERE enabled AND deleted_at IS NULL")]
        results = await asyncio.gather(*(self.adapter.probe(w["base_url"]) for w in workers))
        became_online = False
        for w, (ok, kind, detail) in zip(workers, results):
            if ok:
                caps = None
                # after any failure streak, not only after 'offline': a restart shorter than dead_after still changes
                # the version and argv (the rollout badge reads them)
                if w["state"] != "online" or not w["capabilities"] or w["failing_since"] is not None:
                    caps = await self.adapter.capabilities(w["base_url"])
                    if caps is not None:
                        caps["jobs_api"] = await self.adapter.supports_jobs_api(w["base_url"])
                async with self.db.conn() as c:
                    await c.execute(
                        "UPDATE workers SET state = 'online', failing_since = NULL, failure_kind = NULL, offline_since = NULL,"
                        " last_ok_at = now(), capabilities = coalesce($2, capabilities) WHERE id = $1", w["id"], caps)
                became_online |= w["state"] != "online"
                continue
            async with self.db.conn() as c:
                # thresholds are measured from the first failure of the streak, cleared on success (D-11)
                await c.execute("""
                    UPDATE workers SET failing_since = coalesce(failing_since, now()), failure_kind = $2,
                      state = CASE
                        WHEN $2 = 'dead' AND now() - coalesce(failing_since, now()) >= make_interval(secs => $3) THEN 'offline'
                        WHEN $2 = 'busy' AND now() - coalesce(failing_since, now()) >= make_interval(secs => $4) THEN 'busy_unresponsive'
                        ELSE state END,
                      offline_since = CASE WHEN $2 = 'dead' AND now() - coalesce(failing_since, now()) >= make_interval(secs => $3)
                                           THEN coalesce(offline_since, now()) ELSE offline_since END
                    WHERE id = $1""", w["id"], kind, self.s.dead_after, self.s.busy_after)
            log.info("worker %s probe failed (%s): %s", w["id"], kind, detail)
        if became_online and self.wake:
            self.wake.set()
