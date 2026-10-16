"""Stock-ComfyUI-shaped endpoints synthesised per user from the ledger."""
import asyncio
import uuid

import httpx

from comfyfarm.app import create_app
from conftest import add_worker, fast_settings, tiny, wait_status


async def test_stock_endpoints_per_user(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        w = await fakes()
        await add_worker(r, w.url)
        me, other = {"X-ComfyFarm-User": "tester"}, {"X-ComfyFarm-User": "mallory"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            done = (await c.post("/api/prompt", json={"prompt": tiny(1)}, headers=me)).json()["prompt_id"]
            await wait_status(r, done, "completed")
            for _ in range(50):                                            # outputs are collected after completion
                d = (await c.get(f"/api/jobs/{done}", headers=me)).json()
                if d["outputs"]:
                    break
                await asyncio.sleep(0.1)
            assert d["status"] == "completed" and d["outputs_count"] == 1 and d["preview_output"]["filename"].startswith("fake_")
            assert d["execution_status"]["status_str"] == "success" and d["workflow"]["prompt"]["1"]["class_type"] == "EmptyImage"

            long = (await c.post("/api/prompt", json={"prompt": tiny(2, blur=25, size=2048)}, headers=me)).json()["prompt_id"]
            await wait_status(r, long, "running")
            queued = (await c.post("/api/prompt", json={"prompt": tiny(3)}, headers=me)).json()["prompt_id"]
            q = (await c.get("/api/queue", headers=me)).json()
            assert [x[1] for x in q["queue_running"]] == [long] and [x[1] for x in q["queue_pending"]] == [queued]
            assert (await c.get("/api/queue", headers=other)).json() == {"queue_running": [], "queue_pending": []}

            lst = (await c.get("/api/jobs", params={"status": "completed", "limit": 10}, headers=me)).json()
            assert [j["id"] for j in lst["jobs"]] == [done] and lst["pagination"]["total"] == 1
            assert (await c.get("/api/jobs", params={"status": "bogus"}, headers=me)).status_code == 400
            assert (await c.get(f"/api/jobs/{done}", headers=other)).status_code == 404       # no cross-user reads

            await c.post("/api/queue", json={"delete": [queued]}, headers=me)
            await c.post("/api/interrupt", json={}, headers=me)               # stock: interrupt my running prompt
            await wait_status(r, queued, "cancelled")
            await wait_status(r, long, "cancelled", timeout=20)

            h = (await c.get("/api/history", headers=me)).json()
            assert h[done]["status"]["completed"] is True and h[long]["status"]["messages"][0][0] == "execution_interrupted"
            await c.post("/api/history", json={"delete": [done]}, headers=me)
            assert done not in (await c.get("/api/history", headers=me)).json()

            oi = (await c.get("/api/object_info", headers=me)).json()
            assert "EmptyImage" in oi
            async with r.db.conn() as conn:
                assert await conn.fetchval("SELECT node_count FROM object_info_snapshots") == len(oi)
            await w.stop()                                                     # stale-if-error: snapshot still served
            assert "EmptyImage" in (await c.get("/api/object_info/EmptyImage", headers=me)).json()
