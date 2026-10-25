"""M3: monitoring contract (/api/health/dispatch, /metrics) and dashboard queue operations."""
import asyncio
import uuid

import httpx
import pytest

from comfyfarm.app import Router, create_app
from comfyfarm.core import jobs as jobsvc
from comfyfarm.health.dispatch import prometheus, snapshot
from conftest import add_worker, fast_settings, free_port, tiny, wait_status

CUDA16 = {"backend": "cuda", "os": "posix", "devices": [{"name": "fake", "type": "cuda", "vram_total": 16 * 2**30}]}


# ---- monitoring contract -------------------------------------------------------------------------------------------
@pytest.fixture
async def ledger(dsn):
    """A router without its loops: the test sets worker and job states directly and nothing moves on its own."""
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    yield r
    await r.stop()


async def online_worker(r, name, group="g1", caps=CUDA16):
    # a closed loopback port, not a made-up host: with `*.invalid` workers being probed, processes this test session
    # forked later (pg_ctl, the probe subprocess) died with SIGSEGV on macOS 27 (3/3; mechanism not pinned down)
    gid, wid = await add_worker(r, f"http://127.0.0.1:{free_port()}", group=group, name=name)
    async with r.db.conn() as c:
        await c.execute("UPDATE workers SET state = 'online', capabilities = $2 WHERE id = $1", wid, caps)
    return gid, wid


async def job(r, owner="u", **kw):
    j, _ = await jobsvc.submit(r.db, owner=owner, prompt=tiny(), **kw)
    return j["id"]


async def sql(r, q, *a):
    async with r.db.conn() as c:
        return await c.execute(q, *a)


async def snap(r):
    async with r.db.conn() as c:
        return await snapshot(c)


async def test_health_fields_and_dispatcher_rules(ledger):
    r = ledger
    gid, wid = await online_worker(r, "w1")
    g = (await snap(r))["groups"][str(gid)]
    assert set(g) == {"pending", "paused", "running", "idle_workers", "dispatch_idle_seconds", "running_longest_seconds",
                      "unsatisfiable_pending", "rollout_started_at", "inconsistent_workers"}
    assert g["dispatch_idle_seconds"] is None and g["running_longest_seconds"] is None and g["rollout_started_at"] is None

    ok = await job(r, group_id=gid)
    ungrouped = await job(r)                                            # any group may take it (D-03)
    paused = await job(r, group_id=gid)
    await jobsvc.pause(r.db, paused)
    too_big = await job(r, group_id=gid, requirements={"min_vram_gb": 64})
    ungrouped_rocm = await job(r, requirements={"backend": ["rocm"]})
    s = await snap(r)
    g = s["groups"][str(gid)]
    assert (g["pending"], g["paused"], g["unsatisfiable_pending"], g["idle_workers"]) == (2, 1, 1, 1)
    assert s["ungrouped_unsatisfiable_pending"] == 1
    assert g["dispatch_idle_seconds"] is not None                       # idle worker + job it could take

    # negative control: only an unsatisfiable job left → not a dispatch stall (the old definition reported one)
    for j in (ok, ungrouped):
        await jobsvc.request_cancel(r.db, r.adapter, j)
    g = (await snap(r))["groups"][str(gid)]
    assert g["pending"] == 0 and g["unsatisfiable_pending"] == 1 and g["dispatch_idle_seconds"] is None
    await jobsvc.request_cancel(r.db, r.adapter, ungrouped_rocm)
    assert (await snap(r))["ungrouped_unsatisfiable_pending"] == 0


async def test_stall_clock_starts_when_the_job_became_waitable(ledger):
    r = ledger
    gid, wid = await online_worker(r, "w1")
    j = await job(r, group_id=gid)
    await jobsvc.pause(r.db, j)
    await sql(r, "UPDATE jobs SET created_at = now() - interval '1 hour'")
    await sql(r, "UPDATE job_events SET at = now() - interval '1 hour'")
    await jobsvc.resume(r.db, j)
    stall = (await snap(r))["groups"][str(gid)]["dispatch_idle_seconds"]
    assert stall is not None and stall < 60        # created_at would say 3600: a resumed job is not an hour of stall

    # last dispatch of the group bounds it too: min(now - last dispatch, oldest wait)
    await sql(r, "UPDATE job_events SET at = now() - interval '1 hour'")
    other = await job(r, group_id=gid)
    await sql(r, "UPDATE job_events SET at = now() - interval '1 hour' WHERE job_id = $1", other)
    await sql(r, "UPDATE jobs SET status = 'completed', worker_id = $2, dispatched_at = now() - interval '10 seconds' "
                 "WHERE id = $1", other, wid)
    stall = (await snap(r))["groups"][str(gid)]["dispatch_idle_seconds"]
    assert 9 <= stall < 30

    # no idle worker → null, never 0
    await sql(r, "UPDATE workers SET maintenance = true")
    g = (await snap(r))["groups"][str(gid)]
    assert g["idle_workers"] == 0 and g["dispatch_idle_seconds"] is None


async def test_running_longest_by_worker_group(ledger):
    r = ledger
    gid, wid = await online_worker(r, "w1")
    _, wid2 = await online_worker(r, "w2")
    a = await job(r)                                                    # ungrouped, but running on a g1 worker
    b = await job(r, group_id=gid)
    await sql(r, "UPDATE jobs SET status = 'running', worker_id = $2, dispatched_at = now() - interval '200 seconds', "
                 "started_at = now() - interval '50 seconds' WHERE id = $1", a, wid)
    await sql(r, "UPDATE jobs SET status = 'dispatching', worker_id = $2, dispatched_at = now() - interval '100 seconds' "
                 "WHERE id = $1", b, wid2)
    g = (await snap(r))["groups"][str(gid)]
    assert g["running"] == 2
    assert 99 <= g["running_longest_seconds"] < 120     # dispatching counts from dispatch, running from start (not 200)


async def test_metrics_mirror_json_and_null_stays_absent(dsn, monkeypatch):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        gid, _ = await online_worker(r, "w1")
        gid2, _ = await online_worker(r, "w2", group="g2")
        await sql(r, "UPDATE workers SET maintenance = true WHERE name = 'w2'")
        await job(r, group_id=gid2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            js = (await c.get("/api/health/dispatch")).json()
            text = (await c.get("/metrics")).text
            assert "comfyfarm_ledger_up 1" in text
            assert f'comfyfarm_pending_jobs{{group="{gid2}"}} 1' in text
            assert f'comfyfarm_idle_workers{{group="{gid}"}} 1' in text
            assert js["groups"][str(gid2)]["dispatch_idle_seconds"] is None
            assert "comfyfarm_dispatch_idle_seconds{" not in text            # null → absent, not 0
            assert "comfyfarm_rollout_started_timestamp_seconds{" not in text

            async def broken(_c, **_kw):
                raise OSError("ledger gone")
            monkeypatch.setattr("comfyfarm.app.dispatch_snapshot", broken)
            down = await c.get("/metrics")
            assert down.status_code == 200 and "comfyfarm_ledger_up 0" in down.text
            assert "comfyfarm_pending_jobs" not in down.text                  # nothing that could read as "fine"


def test_prometheus_values():
    s = {"groups": {"3": {"pending": 2, "paused": 0, "running": 1, "idle_workers": 0, "dispatch_idle_seconds": None,
                          "running_longest_seconds": 12.5, "unsatisfiable_pending": 0, "rollout_started_at": None,
                          "inconsistent_workers": 2}},
         "ungrouped_unsatisfiable_pending": 0}
    t = prometheus(s)
    assert 'comfyfarm_running_longest_seconds{group="3"} 12.5' in t and 'comfyfarm_paused_jobs{group="3"} 0' in t
    assert 'comfyfarm_group_inconsistent_workers{group="3"} 2' in t
    assert "comfyfarm_dispatch_idle_seconds{" not in t
    assert prometheus(None).strip().endswith("comfyfarm_ledger_up 0")


# ---- queue operations --------------------------------------------------------------------------------------------
def H(user):
    return {"X-ComfyFarm-User": user}


@pytest.fixture
async def farm_api(dsn):
    app = create_app(fast_settings(dsn))       # no workers: nothing is dispatched, the queue stays put
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield app.state.router, c


async def post_prompt(c, user, seed):
    return (await c.post("/api/prompt", json={"prompt": tiny(seed)}, headers=H(user))).json()["prompt_id"]


async def queue_order(c):
    q = (await c.get("/api/farm/queue?scope=all", headers=H("admin"))).json()["jobs"]
    return [j["prompt_id"] for j in q]


async def test_pause_resume_permissions(farm_api):
    r, c = farm_api
    a = await post_prompt(c, "alice", 1)
    assert (await c.post(f"/api/farm/jobs/{a}/pause", headers=H("bob"))).status_code == 404      # not bob's
    assert (await c.post(f"/api/farm/jobs/{a}/pause", headers=H("alice"))).status_code == 200
    assert (await c.post(f"/api/farm/jobs/{a}/pause", headers=H("alice"))).status_code == 409    # already paused
    q = (await c.get("/api/queue", headers=H("alice"))).json()
    assert [x[1] for x in q["queue_pending"]] == [a]                    # stock view: paused still shows as pending
    assert (await c.post(f"/api/farm/jobs/{a}/resume", headers=H("admin"))).status_code == 200   # admin may
    async with r.db.conn() as conn:
        assert await conn.fetchval("SELECT status FROM jobs WHERE prompt_id = $1", uuid.UUID(a)) == "pending"
        reasons = [x["reason"] for x in await conn.fetch(
            "SELECT reason FROM job_events e JOIN jobs j ON j.id = e.job_id WHERE j.prompt_id = $1 ORDER BY e.id", uuid.UUID(a))]
    assert reasons == ["submitted", "paused", "resumed"]


async def test_reorder_is_admin_only_and_exact(farm_api):
    r, c = farm_api
    a, b, d, e = [await post_prompt(c, u, i) for i, u in enumerate(["alice", "alice", "bob", "alice"])]
    assert await queue_order(c) == [a, b, d, e]
    assert (await c.post(f"/api/farm/jobs/{e}/move", json={"to": "top"}, headers=H("alice"))).status_code == 403
    assert await queue_order(c) == [a, b, d, e]

    async def mv(p, **body):
        res = await c.post(f"/api/farm/jobs/{p}/move", json=body, headers=H("admin"))
        assert res.status_code == 200, res.text
        return await queue_order(c)
    assert await mv(e, to="top") == [e, a, b, d]
    assert await mv(a, to="bottom") == [e, b, d, a]
    assert await mv(a, before=b) == [e, a, b, d]
    assert await mv(e, after=d) == [a, b, d, e]
    assert await mv(d, before=a) == [d, a, b, e]
    await c.post(f"/api/farm/jobs/{b}/pause", headers=H("alice"))      # paused jobs keep their place and can move
    assert await mv(b, to="top") == [b, d, a, e]
    # a later "front" submit still lands on top after a renumbering move
    f = (await c.post("/api/prompt", json={"prompt": tiny(9), "front": True}, headers=H("bob"))).json()["prompt_id"]
    assert (await queue_order(c))[0] == f
    # moving relative to a finished job is refused
    await c.post(f"/api/farm/jobs/{d}/cancel", headers=H("bob"))
    assert (await c.post(f"/api/farm/jobs/{a}/move", json={"before": d}, headers=H("admin"))).status_code == 409
    assert (await c.post(f"/api/farm/jobs/{a}/move", json={"before": a}, headers=H("admin"))).status_code == 400


async def test_queue_scope_and_places(farm_api):
    r, c = farm_api
    a1 = await post_prompt(c, "alice", 1)
    b1 = await post_prompt(c, "bob", 2)
    a2 = await post_prompt(c, "alice", 3)
    mine = (await c.get("/api/farm/queue?scope=all", headers=H("alice"))).json()   # not admin: "all" is ignored
    assert mine["scope"] == "mine" and mine["farm"] == {"queued": 3, "running": 0}
    assert [(j["prompt_id"], j["place"]) for j in mine["jobs"]] == [(a1, 1), (a2, 3)]   # bob's job counts for places
    every = (await c.get("/api/farm/queue?scope=all", headers=H("admin"))).json()
    assert [j["prompt_id"] for j in every["jobs"]] == [a1, b1, a2]
    dash = (await c.get("/api/farm/dashboard", headers=H("alice"))).json()
    assert [j["place"] for j in dash["queue"]] == [1, 3] and dash["recent"] == []


async def test_notes(farm_api):
    r, c = farm_api
    a = await post_prompt(c, "alice", 1)
    assert (await c.put(f"/api/farm/jobs/{a}/note", json={"note": "bob's"}, headers=H("bob"))).status_code == 404
    assert (await c.put(f"/api/farm/jobs/{a}/note", json={"note": "  hero shot v3 " + "x" * 600}, headers=H("alice"))).status_code == 200
    note = (await c.get("/api/farm/queue", headers=H("alice"))).json()["jobs"][0]["note"]
    assert note.startswith("hero shot v3 x") and len(note) == jobsvc.NOTE_MAX
    await c.put(f"/api/farm/jobs/{a}/note", json={"note": "   "}, headers=H("alice"))
    assert (await c.get("/api/farm/queue", headers=H("alice"))).json()["jobs"][0]["note"] is None
    wid = (await c.post("/api/admin/workers", json={"name": "w", "base_url": f"http://127.0.0.1:{free_port()}"}, headers=H("admin"))).json()["id"]
    assert (await c.patch(f"/api/admin/workers/{wid}", json={"note": "fan noisy"}, headers=H("alice"))).status_code == 403
    await c.patch(f"/api/admin/workers/{wid}", json={"note": "fan noisy"}, headers=H("admin"))
    w = (await c.get("/api/admin/workers", headers=H("admin"))).json()[0]
    assert w["note"] == "fan noisy" and "no group: receives no jobs" in w["warnings"]


async def test_admin_cancel_and_retry_copies(farm_api):
    r, c = farm_api
    a = await post_prompt(c, "alice", 7)
    assert (await c.post(f"/api/farm/jobs/{a}/retry", headers=H("alice"))).status_code == 409   # not finished
    assert (await c.post(f"/api/farm/jobs/{a}/cancel", headers=H("bob"))).status_code == 404
    assert (await c.post(f"/api/farm/jobs/{a}/cancel", headers=H("admin"))).json()["status"] == "cancelled"
    other = await post_prompt(c, "bob", 8)
    res = await c.post(f"/api/farm/jobs/{a}/retry", headers=H("admin"))
    assert res.status_code == 200
    new = res.json()["prompt_id"]
    assert new != a
    async with r.db.conn() as conn:
        row = await conn.fetchrow("SELECT j.*, p.prompt FROM jobs j JOIN job_payloads p ON p.job_id = j.id WHERE prompt_id = $1",
                                  uuid.UUID(new))
        old_id = await conn.fetchval("SELECT id FROM jobs WHERE prompt_id = $1", uuid.UUID(a))
    assert row["owner"] == "alice" and row["status"] == "pending" and row["retry_of"] == old_id
    assert row["prompt"] == tiny(7)
    assert await queue_order(c) == [other, new]                         # appended, not jumping the queue


async def test_paused_job_is_not_dispatched_and_retry_runs(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            p = await post_prompt(c, "alice", 3)
            await c.post(f"/api/farm/jobs/{p}/pause", headers=H("alice"))
            w = await fakes(ws_fix=True)
            await add_worker(r, w.url)
            await asyncio.sleep(2.0)
            assert w.fake.post_log == []                                # negative control for "paused is skipped"
            await c.post(f"/api/farm/jobs/{p}/resume", headers=H("alice"))
            await wait_status(r, p, "completed")
            n = (await c.post(f"/api/farm/jobs/{p}/retry", headers=H("alice"))).json()["prompt_id"]
            await wait_status(r, n, "completed")
            assert w.fake.post_log == [p, n]
            recent = (await c.get("/api/farm/dashboard", headers=H("alice"))).json()["recent"]
            assert {x["prompt_id"] for x in recent} == {p, n} and all(x["run_seconds"] is not None for x in recent)


async def test_dashboard_is_served(farm_api):
    r, c = farm_api
    assert (await c.get("/comfyfarm/dashboard")).status_code == 307
    page = await c.get("/comfyfarm/dashboard/")
    assert page.status_code == 200 and "app.js" in page.text
    js = await c.get("/comfyfarm/dashboard/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"      # modules: revalidated every load
    # the typeface comes from the router itself (a closed network has no font CDN) and may be cached for a day
    font = await c.get("/comfyfarm/dashboard/fonts/PretendardVariable.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2" and font.headers["cache-control"] == "public, max-age=86400"
    assert "Pretendard" in (await c.get("/comfyfarm/dashboard/app.css")).text
    assert (await c.get("/comfyfarm/dashboard/fonts/LICENSE-Pretendard.txt")).status_code == 200
