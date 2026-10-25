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


# ---- dispatch / reconcile edges found in review --------------------------------------------------------------------
async def quiet(router_factory, **kw):
    """A router whose loops are stopped: the test drives the ticks itself, so nothing moves on its own."""
    r = await router_factory(**kw)
    for t in r.tasks:
        t.cancel()
    r.dispatcher.ready = lambda wid: True
    return r


async def set_worker(r, wid, **kw):
    async with r.db.conn() as c:
        for k, v in kw.items():
            await c.execute(f"UPDATE workers SET {k} = $2 WHERE id = $1", wid, v)


async def job_row(r, jid):
    async with r.db.conn() as c:
        return dict(await c.fetchrow("SELECT status, worker_id, error, outputs FROM jobs WHERE id = $1", jid))


async def drain(r):
    while r.dispatcher.inflight:
        await asyncio.gather(*list(r.dispatcher.inflight), return_exceptions=True)


@pytest.fixture
async def foreign():
    """Something that is not ComfyUI answering 200 on a worker's address (a login page in front of it, `null`)."""
    from aiohttp import web
    from conftest import free_port
    runners = []

    async def start(body: str, content_type: str):
        app = web.Application()

        async def any_path(_req):
            return web.Response(text=body, content_type=content_type)
        app.router.add_route("*", "/{tail:.*}", any_path)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)
        return f"http://127.0.0.1:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.mark.parametrize("body,ctype", [("<html>login</html>", "text/html"), ("null", "application/json")])
async def test_foreign_answer_from_one_worker_does_not_stop_reconcile(router_factory, fakes, foreign, body, ctype):
    """A worker answering 200 with something that is not a JSON object is 'no answer' for that worker only; the other
    worker's restarted-and-forgotten job is still judged lost in the same tick."""
    r = await quiet(router_factory)
    good = await fakes()
    _, wbad = await add_worker(r, await foreign(body, ctype), name="bad")
    _, wgood = await add_worker(r, good.url, name="good")
    ids = []
    async with r.db.conn() as c:
        for wid in (wbad, wgood):
            j, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
            await c.execute("UPDATE jobs SET status = 'running', worker_id = $2, dispatched_at = now() - interval '1 hour', "
                            "dispatch_ended_at = now() - interval '1 hour', started_at = now() - interval '1 hour' "
                            "WHERE id = $1", j["id"], wid)
            ids.append(j["id"])
    await r.reconciler.tick()                                     # must not raise
    assert (await job_row(r, ids[1]))["error"]["kind"] == "lost"
    assert (await job_row(r, ids[0]))["status"] == "running"      # no answer is not evidence of anything


async def test_html_200_to_post_prompt_is_not_an_acceptance(router_factory, foreign):
    from comfyfarm.adapter.comfy import Accepted, Ambiguous
    r = await quiet(router_factory)
    out = await r.adapter.submit(await foreign("<html>login</html>", "text/html"), str(uuid.uuid4()), tiny(), {}, "c")
    assert not isinstance(out, Accepted) and isinstance(out, Ambiguous), out


async def test_unexpected_error_asking_one_worker_does_not_stop_reconcile(router_factory, fakes):
    """Defence in depth behind the adapter: whatever goes wrong asking one worker stays with that worker."""
    r = await quiet(router_factory)
    good = await fakes()
    _, wbad = await add_worker(r, "http://127.0.0.1:9", name="bad")
    _, wgood = await add_worker(r, good.url, name="good")
    ids = []
    async with r.db.conn() as c:
        for wid in (wbad, wgood):
            j, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
            await c.execute("UPDATE jobs SET status = 'running', worker_id = $2, dispatched_at = now() - interval '1 hour', "
                            "dispatch_ended_at = now() - interval '1 hour', started_at = now() - interval '1 hour' "
                            "WHERE id = $1", j["id"], wid)
            ids.append(j["id"])
    real = r.adapter.job_state

    async def job_state(base_url, *a):
        if base_url.endswith(":9"):
            raise RuntimeError("adapter bug")
        return await real(base_url, *a)
    r.adapter.job_state = job_state
    await r.reconciler.tick()
    assert (await job_row(r, ids[1]))["error"]["kind"] == "lost"


def gated_submit(r, gates: dict):
    """POSTs to the gated workers hang until their gate opens (a body still uploading: the worker does not know the job
    yet); the rest go through."""
    real = r.adapter.submit

    async def submit(base_url, *a):
        if base_url in gates:
            await gates[base_url].wait()
        return await real(base_url, *a)
    r.adapter.submit = submit


async def test_slow_post_is_not_requeued_while_still_in_flight(router_factory, fakes):
    """A POST that takes longer than any fixed guess must not be judged 'absent' by reconcile and re-queued: the job
    would run twice (P3). Reconcile asks the dispatcher what it is still POSTing instead of guessing from time."""
    r = await quiet(router_factory)
    a, b = await fakes(), await fakes()
    _, wa = await add_worker(r, a.url, name="a")
    _, wb = await add_worker(r, b.url, name="b")
    await set_worker(r, wa, state="online")
    await set_worker(r, wb, state="online", maintenance=True)
    gate = asyncio.Event()
    gated_submit(r, {a.url: gate})
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
    assert await r.dispatcher.tick() == 1
    async with r.db.conn() as c:                    # the POST has been going on for a minute
        await c.execute("UPDATE jobs SET dispatched_at = now() - interval '1 minute' WHERE id = $1", job["id"])
    await r.reconciler.tick(); await asyncio.sleep(1.2); await r.reconciler.tick()   # "absent" twice, settled
    assert (await job_row(r, job["id"]))["status"] == "dispatching"
    await set_worker(r, wb, maintenance=False)
    await set_worker(r, wa, maintenance=True)
    assert await r.dispatcher.tick() == 0           # nothing to take: the job is still on its way to A
    gate.set()
    await drain(r)
    pid = str(job["prompt_id"])
    assert a.fake.post_log == [pid] and b.fake.post_log == []
    row = await job_row(r, job["id"])
    assert row["status"] in ("running", "completed") and row["worker_id"] == wa


async def test_late_acceptance_does_not_take_a_job_handed_to_another_worker(router_factory, fakes):
    """If the job was re-queued and claimed by B while A's POST was in flight (another router's reconcile did it), A's
    late 200 must not move B's dispatch to running, and the copy A accepted is cancelled — nobody tracks it."""
    r = await quiet(router_factory)
    a, b = await fakes(), await fakes()
    _, wa = await add_worker(r, a.url, name="a")
    _, wb = await add_worker(r, b.url, name="b")
    await set_worker(r, wa, state="online")
    await set_worker(r, wb, state="online", maintenance=True)
    gates = {a.url: asyncio.Event(), b.url: asyncio.Event()}
    gated_submit(r, gates)
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny(blur=25, size=2048))    # one 4 s step on the fake
    jid, pid = job["id"], str(job["prompt_id"])
    assert await r.dispatcher.tick() == 1
    to_a = set(r.dispatcher.inflight)
    async with r.db.conn() as c:
        async with c.transaction():
            await jobsvc.transition(c, jid, "pending", ("dispatching",), "worker never received it (other router)",
                                    worker_id=None, dispatched_at=None)
    await set_worker(r, wb, maintenance=False)
    await set_worker(r, wa, maintenance=True)
    assert await r.dispatcher.tick() == 1
    to_b = set(r.dispatcher.inflight) - to_a
    gates[a.url].set()
    await asyncio.gather(*to_a)
    row = await job_row(r, jid)
    assert row["status"] == "dispatching" and row["worker_id"] == wb, row
    assert a.fake.jobs[pid].interrupt or a.fake.jobs[pid].status == "cancelled"      # the orphan copy is stopped
    gates[b.url].set()
    await asyncio.gather(*to_b)
    row = await job_row(r, jid)
    assert row["status"] == "running" and row["worker_id"] == wb
    async with r.db.conn() as c:
        cur = {x["id"]: x["current_job_id"] for x in await c.fetch("SELECT id, current_job_id FROM workers")}
    assert cur == {wa: None, wb: jid}


async def test_post_has_an_overall_deadline(router_factory, monkeypatch):
    """httpx timeouts are per phase: an answer trickling in a byte at a time never times out. The adapter bounds the
    whole POST, and the outcome is Ambiguous (the worker may have queued it), never a hang."""
    from aiohttp import web
    from comfyfarm.adapter import comfy
    from conftest import free_port
    r = await quiet(router_factory)

    async def trickle(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        for _ in range(100):
            await resp.write(b" ")
            await asyncio.sleep(0.2)
        return resp
    app = web.Application()
    app.router.add_post("/prompt", trickle)
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    monkeypatch.setattr(comfy, "DISPATCH_DEADLINE", 1.0, raising=False)
    try:
        out = await asyncio.wait_for(r.adapter.submit(f"http://127.0.0.1:{port}", str(uuid.uuid4()), tiny(), {}, "c"), 5)
        assert isinstance(out, comfy.Ambiguous), out
    finally:
        await runner.cleanup()


async def test_credentials_survive_a_requeue(router_factory, fakes):
    """D-29 credentials are forwarded in the POST that the worker accepts — not lost when the first attempt never
    reached its worker and the job went back to pending — and forgotten once a worker has the job."""
    r = await quiet(router_factory)
    b = await fakes()
    _, wa = await add_worker(r, "http://127.0.0.1:9", name="dead")
    _, wb = await add_worker(r, b.url, name="b")
    await set_worker(r, wa, state="online")
    await set_worker(r, wb, state="online", maintenance=True)
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
    r.dispatcher.secrets[job["id"]] = {"auth_token_comfy_org": "SECRET"}
    assert await r.dispatcher.tick() == 1
    await drain(r)
    assert (await job_row(r, job["id"]))["status"] == "pending"           # Unreachable: back to the queue
    await set_worker(r, wb, maintenance=False)
    await set_worker(r, wa, maintenance=True)
    assert await r.dispatcher.tick() == 1
    await drain(r)
    assert b.fake.post_extra[-1].get("auth_token_comfy_org") == "SECRET"
    assert job["id"] not in r.dispatcher.secrets


async def test_credentials_of_a_job_cancelled_before_dispatch_are_dropped(router_factory):
    r = await quiet(router_factory)
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
    r.dispatcher.secrets[job["id"]] = {"api_key_comfy_org": "SECRET"}
    assert await jobsvc.request_cancel(r.db, r.adapter, job["id"]) == "cancelled"
    await r.dispatcher.tick()
    assert r.dispatcher.secrets == {}


async def test_cancel_during_a_dispatch_that_cannot_connect_ends_cancelled(router_factory):
    """Cancelled while its POST was failing to connect: the worker never saw the job, so it is cancelled right away
    and the worker is freed — not left `cancelling` until it fails as worker_lost minutes later."""
    r = await quiet(router_factory)
    dead = "http://127.0.0.1:9"
    _, wid = await add_worker(r, dead, name="dead")
    await set_worker(r, wid, state="online")
    gate = asyncio.Event()
    gated_submit(r, {dead: gate})
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=tiny())
    assert await r.dispatcher.tick() == 1
    assert await jobsvc.request_cancel(r.db, r.adapter, job["id"]) == "cancelling"
    gate.set()
    await drain(r)
    assert (await job_row(r, job["id"]))["status"] == "cancelled"
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT current_job_id FROM workers WHERE id = $1", wid) is None


async def test_outputs_missed_in_a_websocket_gap_are_completed_from_history(router_factory, fakes):
    """Outputs are recorded per `executed` event (D-31); one lost in a WebSocket gap must not leave the job's outputs
    partial for good: completion merges the worker's /history."""
    r = await quiet(router_factory)
    w = await fakes()
    _, wid = await add_worker(r, w.url)
    await set_worker(r, wid, state="online")
    prompt = {"1": {"class_type": "EmptyImage", "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": 0}},
              "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
              "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "x"}}}
    job, _ = await jobsvc.submit(r.db, owner="tester", prompt=prompt)
    pid = str(job["prompt_id"])
    assert await r.dispatcher.tick() == 1
    await drain(r)
    for _ in range(100):
        if pid in w.fake.jobs and w.fake.jobs[pid].status == "completed":
            break
        await asyncio.sleep(0.05)
    seen = {"images": [{"filename": f"fake_{pid[:8]}.png", "subfolder": "", "type": "temp"}]}
    async with r.db.conn() as c:                         # the hub saw node 2's `executed`, the gap swallowed node 3's
        await c.execute("UPDATE jobs SET outputs = $2 WHERE id = $1", job["id"], {"2": seen})
    await r.reconciler.tick()
    row = await job_row(r, job["id"])
    assert row["status"] == "completed" and set(row["outputs"]) == {"2", "3"}, row
    assert row["outputs"]["3"]["images"][0]["type"] == "output"


async def test_migrations_do_not_block_submits(router_factory):
    """Every Database.start (a second router, any CLI command) takes the migration lock; submits must not wait on it."""
    import asyncpg
    from comfyfarm.db import MIGRATION_LOCK
    r = await quiet(router_factory)
    other = await asyncpg.connect(r.s.dsn)
    try:
        await other.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)          # a migration in progress
        job, created = await asyncio.wait_for(jobsvc.submit(r.db, owner="tester", prompt=tiny()), 3)
        assert created
    finally:
        await other.close()
