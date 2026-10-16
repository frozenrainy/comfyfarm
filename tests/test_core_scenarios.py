"""M1 core scenarios against protocol-accurate fake workers and a real Postgres (docs/design/11-testing.md §3).
Every failure branch of dispatch is exercised; each assertion is about the ledger *and* what the worker saw."""
import asyncio
import uuid

import pytest

from comfyfarm.core import jobs as jobsvc
from conftest import add_worker, tiny, wait_status


async def reasons(router, pid):
    async with router.db.conn() as c:
        return [(x["to_status"], x["reason"]) for x in await c.fetch(
            "SELECT to_status, reason FROM job_events WHERE job_id = (SELECT id FROM jobs WHERE prompt_id=$1) ORDER BY id",
            uuid.UUID(pid))]


async def submit(router, seed=0, owner="tester", **kw):
    job, _ = await jobsvc.submit(router.db, owner=owner, prompt=kw.pop("prompt", tiny(seed)), **kw)
    router.dispatcher.wake.set()
    return str(job["prompt_id"])


async def test_happy_path(router_factory, fakes):
    w = await fakes()
    r = await router_factory()
    await add_worker(r, w.url)
    pid = await submit(r, 1)
    job = await wait_status(r, pid, "completed")
    assert w.fake.post_log == [pid]                         # delivered exactly once
    async with r.db.conn() as c:
        evs = [x["to_status"] for x in await c.fetch(
            "SELECT to_status FROM job_events WHERE job_id = (SELECT id FROM jobs WHERE prompt_id=$1) ORDER BY id", uuid.UUID(pid))]
        assert evs[:3] == ["pending", "dispatching", "running"] and evs[-1] == "completed"
        assert await c.fetchval("SELECT current_job_id FROM workers WHERE id = $1", job["worker_id"]) is None


async def test_4xx_is_final_no_redispatch_loop(router_factory, fakes):
    w = await fakes()
    w.fake.mode = "drop"
    r = await router_factory()
    await add_worker(r, w.url)
    pid = await submit(r, 2)
    job = await wait_status(r, pid, "failed")
    assert job["error"]["kind"] == "rejected" and job["error"]["node_errors"]
    await asyncio.sleep(1.5)                                 # several dispatcher periods
    assert w.fake.post_log == [pid]                          # D-07: exactly one POST, no loop


async def test_read_timeout_is_ambiguous_then_running(router_factory, fakes):
    """D-05: the POST times out (hang 12 s > 10 s read timeout) but the worker accepted it → running, not failed,
    and no second POST."""
    w = await fakes()
    w.fake.mode, w.fake.mode_seconds = "hang", 12
    r = await router_factory()
    await add_worker(r, w.url)
    pid = await submit(r, 3)
    await wait_status(r, pid, ("running", "completed"), timeout=30)
    w.fake.mode = "normal"
    await wait_status(r, pid, "completed", timeout=30)
    assert w.fake.post_log == [pid]
    ev = await reasons(r, pid)
    assert ("running", "accepted") not in ev                                  # the POST itself never answered in time
    assert any(st == "running" and why.startswith("worker reports") for st, why in ev), ev   # reconcile asked the worker


async def test_amnesia_running_job_is_lost_not_requeued(router_factory, fakes):
    """D-06: a worker restart forgets the job → failed(kind=lost) after the grace, never pending again."""
    w = await fakes()
    r = await router_factory()
    await add_worker(r, w.url)
    pid = await submit(r, 4, prompt=tiny(4, blur=25, size=2048))      # fake: one 4 s step
    await wait_status(r, pid, "running")
    w.fake.pending.clear(); w.fake.history.clear(); w.fake.jobs.clear()  # restart without the executor noticing
    job = await wait_status(r, pid, "failed", timeout=20)
    assert job["error"]["kind"] == "lost"
    assert w.fake.post_log.count(pid) == 1
    assert ("pending", "worker never received it") not in await reasons(r, pid)   # never re-queued


async def test_worker_gone_fails_its_jobs(router_factory, fakes):
    """D-06b: the worker disappears for good → its running job fails as worker_lost instead of hanging forever."""
    w = await fakes()
    r = await router_factory()
    await add_worker(r, w.url)
    pid = await submit(r, 5, prompt=tiny(5, blur=25, size=2048))
    await wait_status(r, pid, "running")
    await w.stop()
    job = await wait_status(r, pid, "failed", timeout=20)
    assert job["error"]["kind"] == "worker_lost"


async def test_unreachable_worker_requeues_to_another(router_factory, fakes):
    """Connect refused = never reached the process → pending again and taken by a healthy worker."""
    dead_port_url = "http://127.0.0.1:9"                      # discard port, nothing listens
    good = await fakes()
    r = await router_factory()
    await add_worker(r, dead_port_url, name="dead")
    await add_worker(r, good.url, name="good")
    async with r.db.conn() as c:   # the dead worker looked healthy a moment ago and sorts first for dispatch
        await c.execute("UPDATE workers SET state = 'online', last_ok_at = now() - interval '1 hour' WHERE name = 'dead'")
        await c.execute("UPDATE workers SET state = 'online', last_ok_at = now() WHERE name = 'good'")
        dead_id = await c.fetchval("SELECT id FROM workers WHERE name = 'dead'")
    r.dispatcher.ready = lambda wid: True      # a worker whose event stream looked fine but whose HTTP is gone
    pid = await submit(r, 6)
    await wait_status(r, pid, "completed", timeout=20)
    assert good.fake.post_log == [pid]
    ev = await reasons(r, pid)
    assert any(st == "pending" and why.startswith("unreachable") for st, why in ev), ev     # the branch really ran
    assert ("dispatching", f"claimed by worker {dead_id}") in ev


async def test_maintenance_worker_gets_nothing(router_factory, fakes):
    w = await fakes()
    r = await router_factory()
    _, wid = await add_worker(r, w.url)
    async with r.db.conn() as c:
        await c.execute("UPDATE workers SET maintenance = true WHERE id = $1", wid)
    pid = await submit(r, 7)
    await asyncio.sleep(1.5)
    assert w.fake.post_log == []
    await wait_status(r, pid, "pending")


async def test_idempotent_submit_and_cross_owner_conflict(router_factory):
    r = await router_factory()
    key = str(uuid.uuid4())
    a = await submit(r, 8, idempotency_key=key)
    b = await submit(r, 8, idempotency_key=key)
    assert a == b == key
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT count(*) FROM jobs") == 1
    with pytest.raises(jobsvc.Conflict):                     # D-23: someone else's key → 409, no 500
        await submit(r, 8, owner="mallory", idempotency_key=key)


async def test_two_dispatchers_never_double_dispatch(dsn, fakes):
    """D-02: two router processes on one ledger (e.g. a restart overlap). Loops are stopped and both dispatchers tick
    at the same instant every round, so the contention is real and deterministic rather than left to timing."""
    from comfyfarm.app import Router
    from conftest import fast_settings
    ws = [await fakes() for _ in range(3)]
    r1, r2 = Router(fast_settings(dsn, instance="r1")), Router(fast_settings(dsn, instance="r2"))
    await r1.start(loops=False); await r2.start(loops=False)
    try:
        for i, w in enumerate(ws):
            await add_worker(r1, w.url, name=f"w{i}")
        await r1.health.tick()
        pids = [await submit(r1, 100 + i) for i in range(30)]
        for _ in range(400):
            await asyncio.gather(r1.dispatcher.tick(), r2.dispatcher.tick())
            await asyncio.gather(*r1.dispatcher.inflight, *r2.dispatcher.inflight)
            await r1.reconciler.tick()
            async with r1.db.conn() as c:
                left = await c.fetchval("SELECT count(*) FROM jobs WHERE status <> 'completed'")
            if left == 0:
                break
            await asyncio.sleep(0.05)
        posted = [p for w in ws for p in w.fake.post_log]
        dupes = {p for p in posted if posted.count(p) > 1}
        assert not dupes, f"double dispatch: {dupes}"
        assert sorted(posted) == sorted(pids), f"posted {len(posted)} of {len(pids)}, left {left}"
        assert r1.dispatcher.claimed_total > 0 and r2.dispatcher.claimed_total > 0, \
            (r1.dispatcher.claimed_total, r2.dispatcher.claimed_total)   # both routers really competed
    finally:
        await r1.stop(); await r2.stop()


async def test_negative_control_duplicate_is_detected(fakes):
    """The judge must be able to fail: a deliberately duplicated POST shows up in the same check."""
    w = await fakes()
    import httpx
    pid = str(uuid.uuid4())
    async with httpx.AsyncClient() as c:
        for _ in range(2):
            await c.post(f"{w.url}/prompt", json={"prompt": tiny(9), "prompt_id": pid, "client_id": "x"})
    posted = w.fake.post_log
    assert sorted(posted) != sorted([pid])                    # the check used above would flag this


async def test_cancel_pending_and_running(router_factory, fakes):
    w = await fakes()
    r = await router_factory()
    _, wid = await add_worker(r, w.url)
    running = await submit(r, 20, prompt=tiny(20, blur=25, size=2048))     # one 4 s step on the fake
    await wait_status(r, running, "running")
    queued = await submit(r, 21)
    async with r.db.conn() as c:
        jq = await c.fetchval("SELECT id FROM jobs WHERE prompt_id = $1", uuid.UUID(queued))
        jr = await c.fetchval("SELECT id FROM jobs WHERE prompt_id = $1", uuid.UUID(running))
    assert await jobsvc.request_cancel(r.db, r.adapter, jq) == "cancelled"        # never reached a worker
    assert await jobsvc.request_cancel(r.db, r.adapter, jr) == "cancelling"       # cooperative interrupt (P7)
    await wait_status(r, running, "cancelled", timeout=20)
    assert queued not in w.fake.post_log
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT current_job_id FROM workers WHERE id = $1", wid) is None   # worker freed
