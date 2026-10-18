"""Dispatcher: claim (worker, job) pairs under row locks, POST, classify the outcome (docs/design/03-job-lifecycle.md)."""
from __future__ import annotations

import asyncio
import logging
import uuid

from ..adapter.comfy import Accepted, Ambiguous, ComfyAdapter, Rejected, Unreachable
from ..db import Busy, Database
from .jobs import error_doc, transition
from .settings import Settings

log = logging.getLogger(__name__)

IDLE_WORKERS = """
SELECT id FROM workers
 WHERE enabled AND NOT maintenance AND deleted_at IS NULL AND state = 'online' AND kind = 'gpu'
   AND current_job_id IS NULL AND group_id IS NOT NULL
 ORDER BY last_ok_at NULLS LAST, id"""

# Re-checked under the lock: a worker put into maintenance after the candidate list was read is skipped (D-02).
LOCK_WORKER = """
SELECT w.id, w.group_id, w.base_url, w.capabilities FROM workers w JOIN worker_groups g ON g.id = w.group_id
 WHERE w.id = $1 AND w.enabled AND NOT w.maintenance AND w.deleted_at IS NULL AND w.state = 'online' AND w.kind = 'gpu'
   AND w.current_job_id IS NULL AND comfyfarm_satisfies(w.capabilities, g.requirements)
 FOR UPDATE OF w SKIP LOCKED"""

# Group jobs first, then ungrouped ones (policy 'lowest_priority', D-03); highest priority first; only jobs whose
# own requirements this worker satisfies (docs/design/10) and whose owner may use this group (D-34).
PICK_JOB = """
SELECT id, prompt_id FROM jobs
 WHERE status = 'pending' AND deleted_at IS NULL AND (group_id = $1 OR group_id IS NULL)
   AND (allowed_groups IS NULL OR $1 = ANY(allowed_groups))
   AND comfyfarm_satisfies($2, requirements)
 ORDER BY (group_id IS NULL), priority DESC, id
 LIMIT 1 FOR UPDATE SKIP LOCKED"""


def worker_client_id(instance: str, worker_id: int) -> str:
    """Deterministic per (router instance, worker) so a restarted router keeps receiving events (D-13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"comfyfarm:{instance}:worker:{worker_id}"))


class Dispatcher:
    def __init__(self, db: Database, adapter: ComfyAdapter, settings: Settings):
        self.db, self.adapter, self.s = db, adapter, settings
        self.wake = asyncio.Event()
        self.inflight: set[asyncio.Task] = set()
        self.posting: dict[int, int] = {}       # job id → worker id while this process POSTs it (reconcile must not ask)
        self.claimed_total = 0
        self.ready = lambda worker_id: True     # EventHub replaces this: dispatch only once events can be received
        self.secrets: dict[int, dict] = {}      # job id → credentials kept out of the ledger (D-29)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Busy:
                log.warning("dispatcher: database busy, retrying")
            except Exception:
                log.exception("dispatcher tick failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.s.dispatch_period)
            except TimeoutError:
                pass
            self.wake.clear()

    async def tick(self) -> int:
        """Claim as many pairs as possible and POST them concurrently. Returns the number of claims."""
        async with self.db.conn() as c:
            candidates = [r["id"] for r in await c.fetch(IDLE_WORKERS)]
            await self.prune_secrets(c)
        claimed = 0
        for wid in candidates:
            if not self.ready(wid):
                continue
            claim = await self.claim(wid)
            if claim is None:
                continue
            claimed += 1
            self.claimed_total += 1
            self.posting[claim[0]] = wid
            t = asyncio.create_task(self.deliver(*claim))
            self.inflight.add(t)
            t.add_done_callback(self.inflight.discard)
        return claimed

    async def prune_secrets(self, c):
        """Credentials (D-29) are kept until a worker has the job — through re-queues (unreachable, absent) — and
        dropped once it has it or the job ended any other way (cancelled before dispatch, failed, deleted)."""
        ids = list(self.secrets)
        if not ids:
            return
        keep = {r["id"] for r in await c.fetch("SELECT id FROM jobs WHERE id = ANY($1) AND deleted_at IS NULL "
                                               "AND status IN ('pending','paused','dispatching')", ids)}
        for jid in ids:                       # only the ids asked about: a submit may have added one meanwhile
            if jid not in keep:
                self.secrets.pop(jid, None)

    async def claim(self, worker_id: int):
        async with self.db.conn() as c:
            async with c.transaction():
                w = await c.fetchrow(LOCK_WORKER, worker_id)
                if w is None:
                    return None
                j = await c.fetchrow(PICK_JOB, w["group_id"], w["capabilities"])
                if j is None:
                    return None
                await transition(c, j["id"], "dispatching", ("pending",), f"claimed by worker {worker_id}",
                                 worker_id=worker_id, dispatched_at="now()")
                await c.execute("UPDATE workers SET current_job_id = $2 WHERE id = $1", worker_id, j["id"])
                payload = await c.fetchrow("SELECT prompt, extra_data FROM job_payloads WHERE job_id = $1", j["id"])
        return j["id"], str(j["prompt_id"]), {k: w[k] for k in ("id", "group_id", "base_url")}, payload["prompt"], dict(payload["extra_data"] or {})

    async def deliver(self, job_id: int, prompt_id: str, worker: dict, prompt: dict, extra: dict):
        try:
            await self._deliver(job_id, prompt_id, worker, prompt, extra)
        finally:
            if self.posting.get(job_id) == worker["id"]:
                del self.posting[job_id]

    async def _deliver(self, job_id: int, prompt_id: str, worker: dict, prompt: dict, extra: dict):
        if self.s.preview_method and "preview_method" not in extra:
            extra["preview_method"] = self.s.preview_method                     # P16
        extra.update(self.secrets.get(job_id, {}))      # D-29: never stored, only forwarded; kept until accepted
        out = await self.adapter.submit(worker["base_url"], prompt_id, prompt, extra,
                                        worker_client_id(self.s.instance, worker["id"]))
        orphan = cancel = False
        async with self.db.conn() as c:
            async with c.transaction():
                # While this POST was in flight the job may have been re-queued and handed to another worker (by another
                # router's reconcile, or after a POST that outlived its deadline): only the outcome for the worker the
                # job is with may move it. A copy this worker accepted anyway is an orphan nobody tracks.
                row = await c.fetchrow("SELECT status, worker_id FROM jobs WHERE id = $1 FOR UPDATE", job_id)
                if row is None or row["worker_id"] != worker["id"]:
                    orphan = isinstance(out, (Accepted, Ambiguous))
                    log.warning("job %s left worker %s while its POST was in flight (%s)", job_id, worker["id"],
                                type(out).__name__)
                    out = None
                match out:
                    case Accepted():
                        self.secrets.pop(job_id, None)
                        if not await transition(c, job_id, "running", ("dispatching",), "accepted", dispatch_ended_at="now()"):
                            await c.execute("UPDATE jobs SET dispatch_ended_at = now() WHERE id = $1", job_id)
                            # cancelled while the POST was in flight: the worker only knows the job now
                            cancel = row["status"] == "cancelling"
                    case Rejected():
                        self.secrets.pop(job_id, None)
                        await transition(c, job_id, "failed", ("dispatching", "cancelling"), f"rejected {out.status}",
                                         dispatch_ended_at="now()",
                                         error=error_doc("rejected", out.error.get("message", "workflow rejected"),
                                                         status=out.status, error=out.error, node_errors=out.node_errors))
                    case Unreachable():
                        # never reached the process: the worker is not dispatchable until a probe succeeds again
                        await c.execute("UPDATE workers SET state = 'suspect', failing_since = coalesce(failing_since, now()), "
                                        "failure_kind = 'dead' WHERE id = $1", worker["id"])
                        if row["status"] == "cancelling":
                            # cancelled while this POST failed to connect: the worker never saw the job, nothing to wait for
                            await transition(c, job_id, "cancelled", ("cancelling",), f"cancelled, never reached the worker: "
                                             f"{out.reason}", dispatch_ended_at="now()")
                        elif (attempt := await c.fetchval("UPDATE jobs SET attempt = attempt + 1 WHERE id = $1 "
                                                          "RETURNING attempt", job_id)) >= self.s.max_connect_attempts:
                            self.secrets.pop(job_id, None)
                            await transition(c, job_id, "failed", ("dispatching",), "unreachable", dispatch_ended_at="now()",
                                             error=error_doc("unreachable", out.reason, attempts=attempt))
                        else:
                            await transition(c, job_id, "pending", ("dispatching",), f"unreachable: {out.reason}",
                                             worker_id=None, dispatched_at=None)
                        self.wake.set()
                    case Ambiguous():
                        # may be running already; reconcile asks the worker (D-05). Never re-POST blindly (P3).
                        await c.execute("UPDATE jobs SET dispatch_ended_at = now() WHERE id = $1", job_id)
                        log.warning("dispatch of job %s to worker %s ambiguous: %s", job_id, worker["id"], out.reason)
        if orphan or cancel:
            await self.adapter.cancel(worker["base_url"], prompt_id)
