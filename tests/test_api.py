import uuid

import httpx

from comfyfarm.app import create_app
from conftest import add_worker, fast_settings, tiny


async def test_api_submit_shapes_and_conflicts(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        router = app.state.router
        w = await fakes()
        await add_worker(router, w.url)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            r = await c.post("/api/prompt", json={"prompt": tiny(1), "prompt_id": "NOT-A-UUID"})
            assert r.status_code == 400
            key = str(uuid.uuid4())
            r1 = await c.post("/api/prompt", json={"prompt": tiny(1), "prompt_id": key, "client_id": "b1"})
            r2 = await c.post("/api/prompt", json={"prompt": tiny(1), "prompt_id": key, "client_id": "b1"})
            assert r1.status_code == r2.status_code == 200
            assert r1.json()["prompt_id"] == r2.json()["prompt_id"] == key and r1.json()["node_errors"] == {}
            r3 = await c.post("/api/prompt", json={"prompt": tiny(1), "prompt_id": key}, headers={"X-ComfyFarm-User": "mallory"})
            assert r3.status_code == 409
            r4 = await c.get(f"/api/farm/jobs/{key}", headers={"X-ComfyFarm-User": "mallory"})
            assert r4.status_code == 404                               # no hint that the job exists
            h = (await c.get("/api/health/dispatch")).json()["groups"]
            g = next(iter(h.values()))
            assert set(g) == {"pending", "idle_workers", "dispatch_idle_seconds", "running_longest_seconds", "unsatisfiable_pending"}


async def test_credentials_are_forwarded_not_stored(dsn, fakes):
    """D-29: Comfy.org credentials from extra_data reach the worker but never the ledger."""
    from conftest import wait_status
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        router = app.state.router
        w = await fakes()
        await add_worker(router, w.url)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            pid = (await c.post("/api/prompt", json={"prompt": tiny(5), "extra_data": {
                "auth_token_comfy_org": "SECRET-T", "api_key_comfy_org": "SECRET-K", "extra_pnginfo": {"workflow": {}}}})).json()["prompt_id"]
        await wait_status(router, pid, "completed")
        async with router.db.conn() as c:
            stored = await c.fetchval("SELECT extra_data::text FROM job_payloads p JOIN jobs j ON j.id = p.job_id WHERE j.prompt_id = $1", uuid.UUID(pid))
            dump = await c.fetchval("SELECT string_agg(row_to_json(t)::text, '') FROM (SELECT * FROM jobs) t")
        assert "SECRET" not in stored and "SECRET" not in dump and "extra_pnginfo" in stored
        assert w.fake.post_extra[-1]["auth_token_comfy_org"] == "SECRET-T"
