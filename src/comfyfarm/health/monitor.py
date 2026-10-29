"""Worker health: busy vs dead by elapsed time (D-11), probes in parallel, results applied serially (D-12)."""
from __future__ import annotations

import asyncio
import logging
import time

from ..adapter.comfy import ComfyAdapter
from ..db import Busy, BusyLog, Database
from ..core.settings import Settings
from .memory import MemoryBoard

log = logging.getLogger(__name__)
WAIT_REMIND = 600.0          # a worker that never answered: one more line after this long (a build, or nothing started)


async def _none2():
    return None, None


class HealthMonitor:
    def __init__(self, db: Database, adapter: ComfyAdapter, settings: Settings, wake: asyncio.Event | None = None):
        self.db, self.adapter, self.s, self.wake = db, adapter, settings, wake
        self.busy = BusyLog(log, "health")
        # worker id → last inventory attempt, answered or not: a worker whose /object_info fails (multi-MB, up to 90 s,
        # read serially) must not be asked again every tick
        self.inventory_tried: dict[int, float] = {}
        self.last_failure: dict[int, tuple] = {}            # worker id → (kind, detail) last logged
        self.reminded: set[int] = set()                     # never-answered workers told about after WAIT_REMIND
        self.memory = MemoryBoard()                         # last memory reading per worker (dashboard, /metrics)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Busy as e:
                self.busy(e)
            except Exception:
                log.exception("health tick failed")
            await asyncio.sleep(self.s.health_period)

    def _inventory_due(self, w: dict) -> bool:
        inv = (w["capabilities"] or {}).get("inventory") or {}
        last = max(float(inv.get("at", 0)), self.inventory_tried.get(w["id"], 0.0))
        return time.time() - last > self.s.inventory_period

    async def _inventory(self, w: dict) -> dict | None:
        self.inventory_tried[w["id"]] = time.time()
        return await self.adapter.inventory(w["base_url"])

    async def tick(self):
        async with self.db.conn() as c:
            workers = [dict(r) for r in await c.fetch(
                "SELECT id, name, base_url, state, capabilities, failing_since, last_ok_at, now() AS at FROM workers "
                "WHERE enabled AND deleted_at IS NULL")]
        results = await asyncio.gather(*(self.adapter.probe(w["base_url"]) for w in workers))
        # answering is not working: a worker whose executor died answers /queue and runs nothing (field, dev.21)
        vitals = await asyncio.gather(*(self.adapter.vitals(w["base_url"], (w["capabilities"] or {}).get("extension"))
                                        if ok else _none2() for w, (ok, _, _) in zip(workers, results)))
        for w, (_, mem) in zip(workers, vitals):
            self.memory.update(w["id"], mem, w["name"])
        self.memory.forget({w["id"] for w in workers})
        results = [(False, "broken", why) if why else res for res, (why, _) in zip(results, vitals)]
        became_online = refreshed_one = False
        for w, (ok, kind, detail) in zip(workers, results):
            who = f"{w['name']} (#{w['id']})"
            waited = (w["at"] - w["failing_since"]).total_seconds() if w["failing_since"] else 0.0
            if ok:
                caps = None
                # after any failure streak, not only after 'offline': a restart shorter than dead_after still changes
                # the version and argv (the rollout badge reads them)
                if w["state"] != "online" or not w["capabilities"] or w["failing_since"] is not None:
                    caps = await self.adapter.capabilities(w["base_url"])
                    if caps is not None:
                        caps["jobs_api"] = await self.adapter.supports_jobs_api(w["base_url"])
                        caps["extension"] = await self.adapter.worker_info(w["base_url"])   # docs/design/09
                        caps["inventory"] = await self._inventory(w)                       # group consistency
                elif self._inventory_due(w) and not refreshed_one:
                    # models change on disk without a restart: re-read one stale inventory per tick
                    refreshed_one = True
                    inv = await self._inventory(w)
                    if inv is not None:
                        async with self.db.conn() as c:
                            await c.execute("UPDATE workers SET capabilities = capabilities || jsonb_build_object('inventory', $2::jsonb) "
                                            "WHERE id = $1", w["id"], inv)
                async with self.db.conn() as c:
                    await c.execute(
                        "UPDATE workers SET state = 'online', failing_since = NULL, failure_kind = NULL, offline_since = NULL,"
                        " last_ok_at = now(), capabilities = coalesce($2, capabilities) WHERE id = $1", w["id"], caps)
                was = self.last_failure.pop(w["id"], None)
                self.reminded.discard(w["id"])
                if w["last_ok_at"] is None:
                    log.info("worker %s answers — its first answer%s", who, f", after {waited:.0f} s" if waited else "")
                elif was:
                    log.info("worker %s answers again", who)
                became_online |= w["state"] != "online"
                continue
            async with self.db.conn() as c:
                # thresholds are measured from the first failure of the streak, cleared on success (D-11)
                # refused (its allow-list lacks the router's address): as unusable as a dead one, and the dashboard
                # names the address — the warning, not the state, says why
                new = await c.fetchval("""
                    UPDATE workers SET failing_since = coalesce(failing_since, now()), failure_kind = $2,
                      state = CASE
                        WHEN $2 IN ('dead', 'refused', 'broken') AND now() - coalesce(failing_since, now()) >= make_interval(secs => $3) THEN 'offline'
                        WHEN $2 = 'busy' AND now() - coalesce(failing_since, now()) >= make_interval(secs => $4) THEN 'busy_unresponsive'
                        ELSE state END,
                      offline_since = CASE WHEN $2 IN ('dead', 'refused', 'broken') AND now() - coalesce(failing_since, now()) >= make_interval(secs => $3)
                                           THEN coalesce(offline_since, now()) ELSE offline_since END,
                      capabilities = CASE WHEN $2 IN ('refused', 'broken') THEN coalesce(capabilities, '{}'::jsonb)
                                            || jsonb_build_object(CASE $2 WHEN 'refused' THEN 'refused_as' ELSE 'broken_as' END, $5::text)
                                          ELSE capabilities END
                    WHERE id = $1 RETURNING state""", w["id"], kind, self.s.dead_after, self.s.busy_after, detail)
            if w["last_ok_at"] is None:
                # never answered: it is being set up, built or started (the first start builds the UI worker for a
                # minute or two). Refused, timing out, refused again is all the same news — one line says what is
                # going on, the next is its first answer (QA: a first start printed a handful of failures, each a
                # different error while the port opened); a reminder if it takes long
                if w["id"] not in self.last_failure:
                    log.info("worker %s has not answered yet (%s: %s) — normal while it is set up, built or starting; "
                             "the next line is its first answer", who, kind, detail)
                elif waited >= WAIT_REMIND and w["id"] not in self.reminded:
                    self.reminded.add(w["id"])
                    log.info("worker %s still has not answered after %d min (%s: %s) — if it should be running, see its "
                             "log: docker compose logs ui (the UI worker), journalctl --user -u comfyfarm@<instance> on a "
                             "GPU host", who, waited // 60, kind, detail)
            # a warning when it goes offline or stops answering; the failure itself once (a restart is a few seconds
            # of refused connections — not a warning, and not one line every 5 s)
            elif new != w["state"] and new in ("offline", "busy_unresponsive"):
                log.warning("worker %s %s (%s: %s)", who, new.replace("_", " "), kind, detail)
            elif (kind, detail) != self.last_failure.get(w["id"]):
                log.info("worker %s probe failed (%s): %s", who, kind, detail)
            self.last_failure[w["id"]] = (kind, detail)
        if became_online and self.wake:
            self.wake.set()
