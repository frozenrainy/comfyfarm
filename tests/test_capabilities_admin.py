import asyncio
import uuid

import httpx
import pytest

from comfyfarm.app import create_app
from comfyfarm.core import jobs as jobsvc
from conftest import add_worker, fast_settings, tiny, wait_status


async def submit(router, seed, **kw):
    job, _ = await jobsvc.submit(router.db, owner="tester", prompt=tiny(seed), **kw)
    router.dispatcher.wake.set()
    return str(job["prompt_id"])


async def test_requirements_route_to_the_right_backend(router_factory, fakes):
    cuda = await fakes(backend="cuda", vram_gb=16)
    rocm = await fakes(backend="rocm", vram_gb=32)
    r = await router_factory()
    await add_worker(r, cuda.url, name="cuda")
    await add_worker(r, rocm.url, name="rocm")
    want_rocm = [await submit(r, 10 + i, requirements={"backend": ["rocm"]}) for i in range(4)]
    want_big = await submit(r, 20, requirements={"min_vram_gb": 24})
    for p in want_rocm + [want_big]:
        await wait_status(r, p, "completed")
    assert set(want_rocm) | {want_big} <= set(rocm.fake.post_log)
    assert not (set(want_rocm) | {want_big}) & set(cuda.fake.post_log)          # never on the 16 GB CUDA card


async def test_unsatisfiable_job_waits_visibly(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        w = await fakes(backend="cuda", vram_gb=16)
        gid, _ = await add_worker(r, w.url)
        pid = await submit(r, 30, group_id=gid, requirements={"min_vram_gb": 64})
        await asyncio.sleep(1.5)
        assert w.fake.post_log == []
        await wait_status(r, pid, "pending")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            h = (await c.get("/api/health/dispatch")).json()["groups"][str(gid)]
        assert h["unsatisfiable_pending"] == 1                                    # not stuck silently


async def test_unknown_requirement_is_rejected(router_factory):
    r = await router_factory()
    with pytest.raises(jobsvc.BadRequirements):
        await submit(r, 40, requirements={"gpu_brand": "nvidia"})


async def test_admin_api_and_maintenance(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        w = await fakes()
        admin = {"X-ComfyFarm-User": "admin"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            assert (await c.post("/api/admin/groups", json={"name": "video"})).status_code == 403   # tester ≠ admin
            assert (await c.post("/api/admin/groups", json={"name": "video", "requirements": {"x": 1}}, headers=admin)).status_code == 400
            assert (await c.post("/api/admin/groups", json={"name": "video", "requirements": {"backend": ["cuda"]}}, headers=admin)).status_code == 200
            wid = (await c.post("/api/admin/workers", json={"name": "w1", "base_url": w.url, "group": "video"}, headers=admin)).json()["id"]
            assert (await c.patch(f"/api/admin/workers/{wid}", json={"maintenance": True}, headers=admin)).json() == {"changed": True}
            pid = (await c.post("/api/prompt", json={"prompt": tiny(50)})).json()["prompt_id"]
            await asyncio.sleep(1.5)
            assert w.fake.post_log == []                                           # maintenance: nothing dispatched
            await c.patch(f"/api/admin/workers/{wid}", json={"maintenance": False}, headers=admin)
            await wait_status(app.state.router, pid, "completed")
            ws = (await c.get("/api/admin/workers", headers=admin)).json()
            assert ws[0]["capabilities"]["backend"] == "cuda" and ws[0]["warnings"] == []


async def test_notify_wakes_the_dispatcher(dsn, router_factory, fakes):
    """A submit through another process wakes this router via NOTIFY, long before its 30 s poll. The submitter runs no
    loops of its own, and a negative control without the listener shows the job would otherwise wait."""
    from comfyfarm.app import Router
    w = await fakes()
    r1 = await router_factory(dispatch_period=30)
    submitter = Router(fast_settings(dsn, instance="submitter"))
    await submitter.start(loops=False)
    try:
        await add_worker(r1, w.url)
        await r1.health.tick()
        await asyncio.sleep(1.0)                                # r1 is now in its 30 s wait
        job, _ = await jobsvc.submit(submitter.db, owner="tester", prompt=tiny(60))
        await wait_status(r1, str(job["prompt_id"]), ("running", "completed"), timeout=5)
        assert r1.dispatcher.claimed_total == 1

        r1.tasks[-1].cancel()                                   # negative control: no listener
        await asyncio.sleep(1.0)
        job2, _ = await jobsvc.submit(submitter.db, owner="tester", prompt=tiny(61))
        with pytest.raises(AssertionError):
            await wait_status(r1, str(job2["prompt_id"]), ("running", "completed"), timeout=5)
    finally:
        await submitter.stop()
