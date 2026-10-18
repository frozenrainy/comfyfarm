"""Reconciliation: the worker's view is the truth for jobs that left the router (docs/design/03-job-lifecycle.md)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..adapter.comfy import ComfyAdapter, JobState, NoAnswer
from ..db import Busy, Database
from .jobs import error_doc, transition
from .settings import Settings

log = logging.getLogger(__name__)

ACTIVE = """
SELECT j.id, j.prompt_id, j.status, j.dispatched_at, j.dispatch_ended_at, j.absent_seen_at, j.started_at,
       w.id AS wid, w.base_url, w.state, w.enabled, w.deleted_at, w.offline_since,
       coalesce((w.capabilities->>'jobs_api')::boolean, true) AS jobs_api
  FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id
 WHERE j.status IN ('dispatching','running','cancelling')"""


class Reconciler:
    def __init__(self, db: Database, adapter: ComfyAdapter, settings: Settings, wake: asyncio.Event | None = None):
        self.db, self.adapter, self.s, self.wake = db, adapter, settings, wake

    async def run(self):
        while True:
            try:
                await self.tick()
            except Busy:
                log.warning("reconcile: database busy")
            except Exception:
                log.exception("reconcile tick failed")
            await asyncio.sleep(self.s.reconcile_period)

    async def tick(self):
        async with self.db.conn() as c:
            rows = [dict(r) for r in await c.fetch(ACTIVE)]
            await c.execute("UPDATE workers SET current_job_id = NULL WHERE current_job_id IN "
                            "(SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled','pending','paused'))")
        now = datetime.now(timezone.utc)
        # one worker answering nonsense (or a bug in one branch) must not stop the tick for every other worker
        answers = await asyncio.gather(*(self._ask(r, now) for r in rows), return_exceptions=True)
        changed = False
        async with self.db.conn() as c:
            for r, ans in zip(rows, answers):
                if isinstance(ans, Exception):
                    log.warning("reconcile: asking about job %s failed: %s", r["id"], type(ans).__name__)
                    continue
                try:
                    async with c.transaction():
                        changed |= await self._apply(c, r, ans, now)
                except Exception:
                    log.exception("reconcile of job %s failed", r["id"])
        if changed and self.wake:
            self.wake.set()
        await self.collect_outputs()

    async def collect_outputs(self):
        """Real outputs for recently completed jobs (history synthesis, display paths D-10, /api/view routing)."""
        async with self.db.conn() as c:
            rows = await c.fetch("SELECT j.id, j.prompt_id, w.base_url FROM jobs j JOIN workers w ON w.id = j.worker_id "
                                 "WHERE j.status = 'completed' AND j.outputs IS NULL AND j.finished_at > now() - interval '1 hour' "
                                 "ORDER BY j.finished_at DESC LIMIT 20")
        for r in rows:
            h = await self.adapter.history(r["base_url"], str(r["prompt_id"]))
            outputs = (h or {}).get("outputs")
            async with self.db.conn() as c:     # {} marks "asked, nothing there" so the job is not asked again
                await c.execute("UPDATE jobs SET outputs = $2 WHERE id = $1 AND outputs IS NULL", r["id"], outputs or {})

    async def _ask(self, r: dict, now: datetime):
        if r["wid"] is None or r["deleted_at"] is not None or not r["enabled"]:
            return "gone"
        if r["state"] == "offline" and r["offline_since"] and now - r["offline_since"] >= timedelta(seconds=self.s.worker_lost_after):
            return "gone"
        if r["status"] == "dispatching" and r["dispatch_ended_at"] is None and \
                now - r["dispatched_at"] < timedelta(seconds=15):
            return None                                          # POST still in flight in this process
        return await self.adapter.job_state(r["base_url"], str(r["prompt_id"]), r["jobs_api"])

    async def _apply(self, c, r: dict, ans, now: datetime) -> bool:
        jid, st = r["id"], r["status"]
        if ans is None or isinstance(ans, NoAnswer):
            return False                                         # busy worker is not evidence of anything
        if ans == "gone":                                        # D-06b
            return await transition(c, jid, "failed", ("dispatching", "running", "cancelling"), "worker lost",
                                    error=error_doc("worker_lost", "the worker running this job is gone — rerun if needed"))
        assert isinstance(ans, JobState)
        ended = r["dispatch_ended_at"] or (r["dispatched_at"] + timedelta(seconds=15))
        match ans.status:
            case "pending" | "in_progress":
                fields = {"absent_seen_at": None}
                if ans.status == "in_progress" and r["started_at"] is None:
                    fields["started_at"] = "now()"
                if st == "dispatching":
                    return await transition(c, jid, "running", ("dispatching",), f"worker reports {ans.status}", **fields)
                if st == "cancelling":                       # the cancel request may have been lost: repeat (idempotent)
                    await self.adapter.cancel(r["base_url"], str(r["prompt_id"]), r["jobs_api"])
                if "started_at" in fields:
                    await c.execute("UPDATE jobs SET started_at = coalesce(started_at, now()) WHERE id = $1", jid)
                return False
            case "completed":
                return await transition(c, jid, "completed", ("dispatching", "running", "cancelling"), "worker: completed")
            case "failed":
                return await transition(c, jid, "failed", ("dispatching", "running", "cancelling"), "worker: failed",
                                        error=error_doc("execution", "execution error on the worker",
                                                        detail=ans.raw.get("execution_error")))
            case "cancelled":
                return await transition(c, jid, "cancelled", ("dispatching", "running", "cancelling"), "worker: cancelled")
            case None:
                since_end = now - ended
                if st == "dispatching":                         # D-05: absent twice, ≥ settle after the POST ended
                    if since_end < timedelta(seconds=self.s.absent_settle) or r["absent_seen_at"] is None:
                        await c.execute("UPDATE jobs SET absent_seen_at = coalesce(absent_seen_at, now()) WHERE id = $1", jid)
                        return False
                    return await transition(c, jid, "pending", ("dispatching",), "worker never received it",
                                            worker_id=None, dispatched_at=None, dispatch_ended_at=None, absent_seen_at=None)
                if since_end >= timedelta(seconds=self.s.lost_grace):   # D-06: never back to pending
                    to = "cancelled" if st == "cancelling" else "failed"
                    return await transition(c, jid, to, ("running", "cancelling"), "lost",
                                            error=error_doc("lost", "the worker does not know this job (restarted?) — rerun if needed"))
                return False
        log.warning("job %s: unknown worker status %r", jid, ans.status)
        return False
