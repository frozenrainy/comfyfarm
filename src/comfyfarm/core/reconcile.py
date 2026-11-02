"""Reconciliation: the worker's view is the truth for jobs that left the router (docs/design/03-job-lifecycle.md)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from ..adapter.comfy import DISPATCH_DEADLINE, ComfyAdapter, JobState, NoAnswer
from ..db import Busy, BusyLog, Database
from .jobs import error_doc, retry_gpu_faults, transition
from .settings import Settings

log = logging.getLogger(__name__)

# A POST by another router process (or by this one before a restart) has ended at the latest this long after the claim:
# the adapter bounds it with DISPATCH_DEADLINE; the margin covers the time between the claim commit and the POST.
POST_WINDOW = DISPATCH_DEADLINE + 5.0

ACTIVE = """
SELECT j.id, j.prompt_id, j.status, j.dispatched_at, j.dispatch_ended_at, j.absent_seen_at, j.started_at,
       j.dispatch_ended_at IS NULL AND j.dispatched_at > now() - make_interval(secs => $1) AS maybe_posting,
       w.id AS wid, w.base_url, w.state, w.enabled, w.deleted_at, w.offline_since,
       coalesce((w.capabilities->>'jobs_api')::boolean, true) AS jobs_api
  FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id
 WHERE j.status IN ('dispatching','running','cancelling')"""


class Reconciler:
    def __init__(self, db: Database, adapter: ComfyAdapter, settings: Settings, wake: asyncio.Event | None = None,
                 posting: dict[int, int] | None = None):
        self.db, self.adapter, self.s, self.wake = db, adapter, settings, wake
        self.busy = BusyLog(log, "reconcile")
        self.posting = posting if posting is not None else {}    # the dispatcher's: jobs this process is POSTing now
        self.collected: dict[int, float] = {}                    # job id → when its outputs were read from /history
        self.readopt_done: set[int] = set()                      # lost jobs their worker does not know (D-54)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Busy as e:
                self.busy(e)
            except Exception:
                log.exception("reconcile tick failed")
            await asyncio.sleep(self.s.reconcile_period)

    async def tick(self):
        async with self.db.conn() as c:
            rows = [dict(r) for r in await c.fetch(ACTIVE, POST_WINDOW)]
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
        try:
            await self.readopt()
        except Busy:
            raise
        except Exception:
            log.exception("re-adopting lost jobs failed")
        try:
            if await retry_gpu_faults(self.db, self.s.retry_gpu_faults) and self.wake:     # D-44
                self.wake.set()
        except Busy:
            raise
        except Exception:
            log.exception("automatic retry after a GPU fault failed")

    async def collect_outputs(self):
        """Real outputs of recently completed jobs (history synthesis, display paths D-10, /api/view routing). The
        `executed` events record them as they come (D-31), but events sent during a WebSocket gap are lost, so every
        completed job is read from /history once and merged — the worker's record wins per node."""
        cutoff = time.monotonic() - 3600
        self.collected = {k: t for k, t in self.collected.items() if t > cutoff}
        async with self.db.conn() as c:
            rows = await c.fetch("SELECT j.id, j.prompt_id, w.base_url FROM jobs j JOIN workers w ON w.id = j.worker_id "
                                 "WHERE j.status = 'completed' AND j.finished_at > now() - interval '1 hour' "
                                 "AND NOT (j.id = ANY($1)) ORDER BY j.finished_at DESC LIMIT 20", list(self.collected))
        for r in rows:
            h = await self.adapter.history(r["base_url"], str(r["prompt_id"]))
            outputs = (h or {}).get("outputs")
            async with self.db.conn() as c:     # {} marks "asked, nothing there" for jobs without any event either
                await c.execute("UPDATE jobs SET outputs = coalesce(outputs, '{}'::jsonb) || $2::jsonb WHERE id = $1",
                                r["id"], outputs if isinstance(outputs, dict) else {})
            self.collected[r["id"]] = time.monotonic()                  # asked once per process, answer or not

    async def readopt(self):
        """D-54: a job failed as lost because its worker stopped answering (a host swapping to a crawl) may still be
        running there — field (10-31): the job failed in the ledger while ComfyUI went on with it, and the dashboard
        showed nothing of it. When the worker answers again it is asked: still running or queued → running again (lost
        again later, asked again); finished → the worker's outcome; unknown there → really lost, not asked again. Not
        when the job was retried (D-44) — the retry stands — nor after 24 hours."""
        async with self.db.conn() as c:
            rows = [dict(r) for r in await c.fetch(
                "SELECT j.id, j.prompt_id, j.worker_id, w.base_url, coalesce((w.capabilities->>'jobs_api')::boolean, true) AS jobs_api "
                "FROM jobs j JOIN workers w ON w.id = j.worker_id "
                "WHERE j.status = 'failed' AND j.error->>'kind' IN ('worker_lost', 'lost') AND j.deleted_at IS NULL "
                "AND j.finished_at > now() - interval '24 hours' AND w.state = 'online' AND w.enabled AND w.deleted_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM jobs k WHERE k.retry_of = j.id) AND NOT (j.id = ANY($1)) "
                "ORDER BY j.finished_at DESC LIMIT 10", list(self.readopt_done))]
        for r in rows:
            ans = await self.adapter.job_state(r["base_url"], str(r["prompt_id"]), r["jobs_api"])
            if ans is None or isinstance(ans, NoAnswer):
                continue                                         # busy again: ask next tick
            async with self.db.conn() as c:
                async with c.transaction():
                    match ans.status:
                        case "pending" | "in_progress":
                            if await transition(c, r["id"], "running", ("failed",), f"re-adopted: the worker still has it "
                                                f"({ans.status})", error=None, finished_at=None):
                                log.info("job %s re-adopted: the worker still has it (%s)", r["id"], ans.status)
                                # the worker is busy with it: no new job on top (one already sent there stays queued behind)
                                await c.execute("UPDATE workers SET current_job_id = $2 WHERE id = $1 AND current_job_id IS NULL",
                                                r["worker_id"], r["id"])
                                if self.wake:
                                    self.wake.set()
                        case "completed":
                            await c.execute("UPDATE jobs SET finished_at = NULL WHERE id = $1 AND status = 'failed'", r["id"])
                            if await transition(c, r["id"], "completed", ("failed",), "re-adopted: the worker finished it",
                                                error=None):
                                log.info("job %s re-adopted: the worker finished it after all", r["id"])
                        case "cancelled":
                            await transition(c, r["id"], "cancelled", ("failed",), "re-adopted: cancelled on the worker",
                                             error=None)
                        case "failed":
                            await c.execute("UPDATE jobs SET error = $2 WHERE id = $1 AND status = 'failed'", r["id"],
                                            error_doc("execution", "execution error on the worker",
                                                      detail=ans.raw.get("execution_error")))
                        case _:                                  # the worker does not know it: it really is lost —
                            self.readopt_done.add(r["id"])       # not asked again (any other answer moves it out of the query)

    async def _ask(self, r: dict, now: datetime):
        if r["wid"] is None or r["deleted_at"] is not None or not r["enabled"]:
            return "gone"
        if r["state"] == "offline" and r["offline_since"] and now - r["offline_since"] >= timedelta(seconds=self.s.worker_lost_after):
            return "gone"
        if r["id"] in self.posting:
            return None          # this process is still POSTing it: "absent" now would re-queue a job about to arrive
        if r["status"] == "dispatching" and r["maybe_posting"]:
            return None          # another router process may still be POSTing it (bounded by the deadline)
        return await self.adapter.job_state(r["base_url"], str(r["prompt_id"]), r["jobs_api"])

    async def _apply(self, c, r: dict, ans, now: datetime) -> bool:
        jid, st = r["id"], r["status"]
        if ans is None or isinstance(ans, NoAnswer):
            return False                                         # busy worker is not evidence of anything
        if ans == "gone":                                        # D-06b
            return await transition(c, jid, "failed", ("dispatching", "running", "cancelling"), "worker lost",
                                    error=error_doc("worker_lost", "the worker running this job is gone — rerun if needed"))
        assert isinstance(ans, JobState)
        ended = r["dispatch_ended_at"] or (r["dispatched_at"] + timedelta(seconds=POST_WINDOW))
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
                if since_end >= timedelta(seconds=self.s.lost_grace) and st == "running" and r["started_at"] is None:
                    # D-43: it waited in the worker's queue and never ran (no execution_start, never seen in progress):
                    # a restart lost nothing of it — back in line, not failed (field, dev.21: the job queued behind a
                    # dead executor). One that started stays D-06: never back to pending.
                    return await transition(c, jid, "pending", ("running",), "the worker restarted before running it",
                                            worker_id=None, dispatched_at=None, dispatch_ended_at=None, absent_seen_at=None)
                if since_end >= timedelta(seconds=self.s.lost_grace):   # D-06: never back to pending
                    to = "cancelled" if st == "cancelling" else "failed"
                    return await transition(c, jid, to, ("running", "cancelling"), "lost",
                                            error=error_doc("lost", "the worker does not know this job (restarted?) — rerun if needed"))
                return False
        log.warning("job %s: unknown worker status %r", jid, ans.status)
        return False
