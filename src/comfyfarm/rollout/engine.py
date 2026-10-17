"""Rollout engine (docs/design/07 'Rollout algorithm', contract §7).

Per worker: maintenance on → drained (ledger and worker queue empty, twice, and once more right before the restart)
→ assignment change → restart via the instance's backend → seen **down** (else the restart did nothing) → up with the
new argv (`match`) → verify → maintenance restored. On failure after the assignment changed: revert that worker only
(old profile, restart, `match`); it stays in maintenance. The first failure stops new steps (canary stop); steps
already running finish; the rest are skipped.

State lives in the ledger (`rollouts`, `rollout_steps`), so the dashboard shows it and a router restart cannot lose
it: a rollout found running at start is marked `interrupted` (its workers stay in maintenance) until an admin resumes
or aborts it. Resuming re-runs unfinished steps from the top; a worker already running the target is not restarted.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..adapter.comfy import QUERY_TIMEOUT, classify_exception
from ..db import Busy
from . import backends as be
from . import render as rd

log = logging.getLogger(__name__)

DEFAULTS = {"drain_timeout": 0, "observe_timeout": None, "start_timeout": 900, "verify_timeout": 120, "smoke": False}
FINAL = ("done", "failed", "reverted", "revert_failed", "skipped")
REFILL_DELAY = 4.0             # after a worker returns, before the next leaves: capacity never dips below the window
STATUS_EVERY = 5.0             # backend.status (an ssh call) at most this often per instance
HEARTBEAT = 240.0              # log line while a drain waits (log-based suppression windows must not expire)
BROKEN_WORKFLOW = {"1": {"class_type": "ComfyFarmVerifyNoSuchNode", "inputs": {}}}
SMOKE_WORKFLOW = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0}},
                  "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}}}


class StepFailed(Exception):
    pass


class Aborted(Exception):
    pass


async def audit(c, actor: str, action: str, target: str | None, **detail):
    await c.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES ($1, $2, $3, $4)", actor, action, target, detail)


async def profile_doc(c, profile_id: int) -> dict | None:
    r = await c.fetchrow("SELECT * FROM profiles WHERE id = $1", profile_id)
    return None if r is None else {k: r[k] for k in ("name", "core", "venv", "nodes", "frontend", "flags", "env")} | {"id": r["id"]}


class Engine:
    def __init__(self, router):
        self.r = router
        self.db, self.s = router.db, router.s
        self.make_backend = be.make                      # tests replace it
        self.poll = 1.0                                  # probe interval while watching a restart
        self.start_lock = asyncio.Lock()                 # startups are serialised (shared storage, D: docs/design/07)
        self.current: int | None = None
        self._status_at: dict[str, float] = {}
        self.http = httpx.AsyncClient(timeout=QUERY_TIMEOUT)

    # ---- loop ---------------------------------------------------------------------------------------------------
    async def interrupt_leftovers(self):
        """At start: a rollout that was running belongs to no task any more."""
        async with self.db.conn() as c:
            n = await c.execute("UPDATE rollouts SET status = 'interrupted', error = 'router restarted during the rollout' "
                                "WHERE status IN ('running', 'aborting')")
        if n.split()[-1] != "0":
            log.warning("rollout interrupted by a router restart; its workers stay in maintenance until resumed or aborted")

    async def run(self):
        await self.interrupt_leftovers()
        while True:
            try:
                async with self.db.conn() as c:
                    rid = await c.fetchval("SELECT id FROM rollouts WHERE status IN ('running', 'aborting') ORDER BY id LIMIT 1")
                if rid is not None:
                    self.current = rid
                    try:
                        await self.drive(rid)
                    finally:
                        self.current = None
            except asyncio.CancelledError:
                raise
            except Busy:
                log.warning("rollout: database busy")
            except Exception:
                log.exception("rollout loop failed")
            await asyncio.sleep(1.0)

    async def stop(self):
        await self.http.aclose()

    # ---- one rollout ------------------------------------------------------------------------------------------
    async def drive(self, rid: int):
        async with self.db.conn() as c:
            ro = await c.fetchrow("SELECT * FROM rollouts WHERE id = $1", rid)
        opts = {**DEFAULTS, **(ro["options"] or {})}
        actor = f"rollout #{rid} ({ro['created_by']})"
        active: dict[asyncio.Task, int] = {}
        try:
            await self._drive(rid, ro, opts, actor, active)
        finally:
            # cancelled (router shutting down): the step tasks go with it, as in a crash — never left running
            # unattended; their steps stay where they were and the rollout becomes 'interrupted' at the next start
            for t in active:
                t.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    async def _drive(self, rid: int, ro, opts: dict, actor: str, active: dict):
        failed = False
        last_return = 0.0
        while True:
            async with self.db.conn() as c:
                status = await c.fetchval("SELECT status FROM rollouts WHERE id = $1", rid)
            stopping = failed or status == "aborting"
            if not stopping and len(active) < ro["window_size"] and time.monotonic() - last_return >= REFILL_DELAY:
                step = await self.next_step(rid, set(active.values()))
                if step is not None:
                    t = asyncio.create_task(self.run_step(step, opts, actor, lambda: self.aborting(rid)))
                    active[t] = step["id"]
                    continue
            if not active:
                async with self.db.conn() as c:
                    left = await c.fetchval("SELECT count(*) FROM rollout_steps WHERE rollout_id = $1 AND status = 'pending'", rid)
                if stopping or not left:
                    break
            done, _ = await asyncio.wait(active, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                active.pop(t)
                ok = t.result() if not t.cancelled() and t.exception() is None else False
                if t.exception() is not None:
                    log.error("rollout step crashed: %r", t.exception())
                failed |= ok is False
                last_return = time.monotonic()
        await self.finish(rid, actor)

    async def aborting(self, rid: int) -> bool:
        async with self.db.conn() as c:
            return await c.fetchval("SELECT status FROM rollouts WHERE id = $1", rid) == "aborting"

    async def next_step(self, rid: int, running_ids: set[int]):
        """Among pending steps: the worker whose running job finishes soonest (`elapsed × (100 − p) / p`), idle first."""
        async with self.db.conn() as c:
            rows = await c.fetch(
                "SELECT s.*, j.started_at AS job_started, j.progress AS job_progress, w.current_job_id "
                "FROM rollout_steps s JOIN workers w ON w.id = s.worker_id LEFT JOIN jobs j ON j.id = w.current_job_id "
                "WHERE s.rollout_id = $1 AND s.status IN ('pending', 'draining', 'restarting', 'starting', 'verifying', 'reverting') "
                "ORDER BY s.position", rid)
        now = time.time()

        def remaining(r):
            if r["current_job_id"] is None:
                return 0.0
            st = (r["job_progress"] or {}).get("state") or {}
            p = (st.get("value") or 0) / st["max"] if isinstance(st, dict) and st.get("max") else 0
            if p <= 0 or r["job_started"] is None:
                return 1e9
            elapsed = now - r["job_started"].timestamp()
            return elapsed * (1 - p) / p
        # unfinished steps of an interrupted run come first (their worker is already in maintenance)
        cands = [r for r in rows if r["id"] not in running_ids]
        resumed = [r for r in cands if r["status"] != "pending"]
        if resumed:
            return dict(resumed[0])
        cands = sorted(cands, key=lambda r: (remaining(r), r["position"]))
        return dict(cands[0]) if cands else None

    async def finish(self, rid: int, actor: str):
        async with self.db.conn() as c:
            async with c.transaction():
                aborting = await c.fetchval("SELECT status FROM rollouts WHERE id = $1", rid) == "aborting"
                await c.execute("UPDATE rollout_steps SET status = 'skipped', finished_at = now(), "
                                "detail = coalesce(detail, $2) WHERE rollout_id = $1 AND status = 'pending'",
                                rid, "aborted" if aborting else "not started: an earlier step failed")
                bad = await c.fetchval("SELECT count(*) FROM rollout_steps WHERE rollout_id = $1 AND status IN "
                                       "('failed', 'reverted', 'revert_failed')", rid)
                status = "aborted" if aborting else "failed" if bad else "succeeded"
                await c.execute("UPDATE rollouts SET status = $2, finished_at = now() WHERE id = $1", rid, status)
                await audit(c, actor, f"rollout {status}", f"rollout #{rid}")
                groups = [r["group_id"] for r in await c.fetch(
                    "SELECT DISTINCT w.group_id FROM rollout_steps s JOIN workers w ON w.id = s.worker_id "
                    "WHERE s.rollout_id = $1 AND s.status = 'done' AND w.group_id IS NOT NULL", rid)]
        refresh = getattr(self.r, "refresh_snapshot", None)       # set by the API module
        for gid in groups if refresh else []:  # node definitions may have changed: refresh once, after the rollout (D-20)
            try:
                await refresh(gid)
            except Exception as e:
                log.warning("object_info refresh after rollout failed: %s", type(e).__name__)
        log.info("rollout #%s %s", rid, status)

    # ---- one worker ---------------------------------------------------------------------------------------------
    async def set_step(self, sid: int, status: str, detail: str | None = None, error: str | None = None):
        async with self.db.conn() as c:
            await c.execute("UPDATE rollout_steps SET status = $2, detail = $3, error = coalesce($4, error), "
                            "started_at = coalesce(started_at, now()), "
                            "finished_at = CASE WHEN $2 = ANY($5::text[]) THEN now() ELSE finished_at END WHERE id = $1",
                            sid, status, detail, error, list(FINAL))

    async def load(self, step: dict):
        async with self.db.conn() as c:
            w = await c.fetchrow("SELECT * FROM workers WHERE id = $1", step["worker_id"])
            inst = await c.fetchrow("SELECT * FROM instances WHERE worker_id = $1", step["worker_id"])
            to = await profile_doc(c, step["to_profile_id"])
            frm = await profile_doc(c, step["from_profile_id"]) if step["from_profile_id"] else None
        if inst is None:
            raise StepFailed("the worker has no instance configuration")
        return dict(w), dict(inst), to, frm

    async def run_step(self, step: dict, opts: dict, actor: str, aborting) -> bool:
        """True if the worker ended on the target profile."""
        sid = step["id"]
        try:
            w, inst, to, frm = await self.load(step)
        except StepFailed as e:
            await self.set_step(sid, "failed", error=str(e))
            return False
        backend = self.make_backend(inst["restart_backend"], inst["restart_config"])
        changed = False
        try:
            async with self.db.conn() as c:
                async with c.transaction():
                    was = await c.fetchval("UPDATE rollout_steps SET was_maintenance = coalesce(was_maintenance, $2) "
                                           "WHERE id = $1 RETURNING was_maintenance", sid, w["maintenance"])
                    if not w["maintenance"]:
                        await c.execute("UPDATE workers SET maintenance = true WHERE id = $1", w["id"])
                        await audit(c, actor, "maintenance on", w["name"])
            await self.set_step(sid, "draining", "waiting for the running job")
            await asyncio.sleep(self.s.dispatch_period + 1.0)          # a claim in flight must land before we look
            await self.drain(w, opts, aborting)
            async with self.start_lock:                                # startups one at a time
                await self.drain(w, opts, aborting, quick=True)        # once more, right before the restart
                target = rd.build(to, inst)
                if await self.argv(w["base_url"]) == target["argv"][1:] and inst["profile_id"] == to["id"]:
                    await self.set_step(sid, "starting", "already running the target")
                else:
                    await self.set_step(sid, "restarting", f"via {inst['restart_backend']}")
                    await self.assign(inst, to, actor)
                    changed = True
                    await self.restart(backend, inst, w, opts)
                    await self.set_step(sid, "starting", "waiting for the new version")
                    await self.wait_match(backend, inst, w, target, opts)
            await self.set_step(sid, "verifying")
            await self.verify(w, opts)
            async with self.db.conn() as c:
                async with c.transaction():
                    await c.execute("UPDATE workers SET maintenance = $2 WHERE id = $1", w["id"], was)
                    if not was:
                        await audit(c, actor, "maintenance off", w["name"])
            await self.set_step(sid, "done", f"running {to['name']}")
            self.r.dispatcher.wake.set()
            return True
        except Aborted:
            await self.undrain(w, sid, actor)
            await self.set_step(sid, "skipped", "aborted before the restart")
            return True                                                 # not a failure
        except (StepFailed, be.BackendError, Exception) as e:     # unexpected errors too: never leave a step half-done
            if not isinstance(e, (StepFailed, be.BackendError)):
                log.exception("rollout step on %s crashed", w["name"])
                e = StepFailed(f"internal error: {type(e).__name__}: {e}")
            log.warning("rollout step on %s failed: %s", w["name"], e)
            if not changed:
                await self.undrain(w, sid, actor)                       # nothing changed: back to work
                await self.set_step(sid, "failed", error=str(e))
                return False
            await self.set_step(sid, "reverting", f"back to {frm['name'] if frm else '?'}", error=str(e))
            try:
                if frm is None:
                    raise StepFailed("no previous profile to return to")
                await self.assign(inst, frm, actor)
                await self.restart(backend, inst, w, opts)
                await self.wait_match(backend, inst, w, rd.build(frm, inst), opts)
                await self.set_step(sid, "reverted", f"back on {frm['name']}; left in maintenance")
            except Exception as e2:
                await self.set_step(sid, "revert_failed", "left in maintenance", error=f"{e} · revert: {type(e2).__name__}: {e2}")
            return False

    async def undrain(self, w: dict, sid: int, actor: str):
        async with self.db.conn() as c:
            was = await c.fetchval("SELECT was_maintenance FROM rollout_steps WHERE id = $1", sid)
            if not was:
                await c.execute("UPDATE workers SET maintenance = false WHERE id = $1", w["id"])
                await audit(c, actor, "maintenance off", w["name"])

    async def assign(self, inst: dict, profile: dict, actor: str):
        async with self.db.conn() as c:
            async with c.transaction():
                old = await c.fetchval("SELECT profile_id FROM instances WHERE worker_id = $1 FOR UPDATE", inst["worker_id"])
                await c.execute("UPDATE instances SET profile_id = $2, updated_at = now() WHERE worker_id = $1",
                                inst["worker_id"], profile["id"])
                await audit(c, actor, "assign", inst["instance"], **{"from": old, "to": profile["id"], "profile": profile["name"]})
        inst["profile_id"] = profile["id"]

    # ---- observations -----------------------------------------------------------------------------------------
    async def drain(self, w: dict, opts: dict, aborting, quick: bool = False):
        """Ledger has no job on the worker AND the worker's own queue is empty — twice in a row (two signals with
        different lag; a job arriving late must be seen)."""
        need, ok_streak, t0, beat = (1 if quick else 2), 0, time.monotonic(), time.monotonic()
        while True:
            if await aborting() and not quick:
                raise Aborted()
            async with self.db.conn() as c:
                busy = await c.fetchval("SELECT count(*) FROM jobs WHERE worker_id = $1 AND status IN "
                                        "('dispatching', 'running', 'cancelling')", w["id"])
            empty = await self.worker_queue_empty(w["base_url"])
            ok_streak = ok_streak + 1 if (not busy and empty is not False) else 0
            if ok_streak >= need:
                return
            if opts["drain_timeout"] and time.monotonic() - t0 > opts["drain_timeout"]:
                raise StepFailed(f"not drained after {opts['drain_timeout']} s")
            if time.monotonic() - beat > HEARTBEAT:
                beat = time.monotonic()
                log.info("rollout: still draining %s (%d s)", w["name"], time.monotonic() - t0)
            await asyncio.sleep(2.0 if not quick else 0.5)

    async def worker_queue_empty(self, base_url: str) -> bool | None:
        """None if the worker does not answer (a dead worker has nothing running)."""
        try:
            r = await self.http.get(f"{base_url}/queue")
            q = r.json()
            return not q.get("queue_running") and not q.get("queue_pending")
        except Exception:
            return None

    async def alive(self, base_url: str) -> str:
        try:
            await self.http.get(f"{base_url}/queue")
            return "up"
        except Exception as e:
            return classify_exception(e)          # 'dead' = nothing listens, 'busy' = process there, loop blocked

    async def argv(self, base_url: str):
        try:
            return (await self.http.get(f"{base_url}/system_stats")).json()["system"]["argv"]
        except Exception:
            return None

    async def refused(self, backend, inst) -> bool:
        """ExecMainStatus "2" = the launcher refused the render. Asked at most every STATUS_EVERY seconds."""
        key = inst["instance"]
        if time.monotonic() - self._status_at.get(key, 0) < STATUS_EVERY:
            return False
        self._status_at[key] = time.monotonic()
        st = await backend.status(key)
        return bool(st) and str(st.get("ExecMainStatus")) == "2"

    async def restart(self, backend, inst: dict, w: dict, opts: dict):
        ok, detail = await backend.restart(inst["instance"])
        if not ok:
            raise StepFailed(f"restart failed: {detail}")
        # the worker must be seen DOWN: a restart call that did nothing must not count (ComfyUI keeps its PID on its
        # own restart, so PIDs and uptimes prove nothing)
        limit = opts["observe_timeout"] or backend.observe_timeout
        t0, dead = time.monotonic(), 0
        while time.monotonic() - t0 < limit:
            dead = dead + 1 if await self.alive(w["base_url"]) == "dead" else 0
            if dead >= 2:
                return
            if await self.refused(backend, inst):
                raise StepFailed("the launcher refused the render (exit 2)")
            await asyncio.sleep(self.poll)
        raise StepFailed(f"the worker was not seen down within {limit:.0f} s: the restart did nothing")

    async def wait_match(self, backend, inst: dict, w: dict, render: dict, opts: dict):
        t0, last = time.monotonic(), None
        while time.monotonic() - t0 < opts["start_timeout"]:
            if await self.refused(backend, inst):
                raise StepFailed("the launcher refused the render (exit 2)")
            last = await self.argv(w["base_url"])
            if last == render["argv"][1:]:
                # badge, version and jobs API current right away (a restart shorter than the health probe interval
                # leaves no failure streak behind that would make the monitor re-read them)
                caps = await self.r.adapter.capabilities(w["base_url"])
                if caps is not None:
                    caps["jobs_api"] = await self.r.adapter.supports_jobs_api(w["base_url"])
                    caps["extension"] = await self.r.adapter.worker_info(w["base_url"])
                async with self.db.conn() as c:
                    await c.execute("UPDATE workers SET capabilities = coalesce($3, capabilities) || jsonb_build_object('argv', $2::jsonb) "
                                    "WHERE id = $1", w["id"], last, caps)
                return
            await asyncio.sleep(self.poll)
        why = "never came back" if last is None else "came back with a different argv (badge: differs)"
        raise StepFailed(f"the worker {why} within {opts['start_timeout']} s")

    async def verify(self, w: dict, opts: dict):
        base, t = w["base_url"], opts["verify_timeout"]
        try:
            r = await self.http.get(f"{base}/object_info", timeout=t)
            if r.status_code != 200 or not isinstance(r.json(), dict) or not r.json():
                raise StepFailed(f"object_info answered {r.status_code}")
            # validation must work: a deliberately broken workflow is refused with 400 (P5), never queued
            r = await self.http.post(f"{base}/prompt", json={"prompt": BROKEN_WORKFLOW}, timeout=t)
            if r.status_code != 400:
                raise StepFailed(f"a broken workflow was answered {r.status_code}, not 400")
        except httpx.HTTPError as e:
            raise StepFailed(f"verify: {type(e).__name__}")
        if opts["smoke"]:
            import uuid
            pid = str(uuid.uuid4())
            r = await self.http.post(f"{base}/prompt", json={"prompt": SMOKE_WORKFLOW, "prompt_id": pid}, timeout=t)
            if r.status_code != 200:
                raise StepFailed(f"smoke workflow refused ({r.status_code})")
            t0 = time.monotonic()
            while time.monotonic() - t0 < t:
                h = (await self.http.get(f"{base}/history/{pid}")).json()
                if pid in h:
                    if h[pid].get("status", {}).get("status_str") == "success":
                        return
                    raise StepFailed("smoke workflow failed")
                await asyncio.sleep(1.0)
            raise StepFailed("smoke workflow did not finish")
