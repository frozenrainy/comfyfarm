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
            # the frontend's queue panel drops the whole list when an item's preview lacks these (ComfyUI's jobs API)
            assert d["preview_output"]["nodeId"] == "3" and d["preview_output"]["mediaType"] == "images"
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


async def test_object_info_group_header_takes_an_id(dsn):
    """Header values must be Latin-1: the extension sends the picked group's id, not its (possibly Korean) name. An
    ASCII name still works; an unknown id or name falls back as before."""
    import gzip
    import json
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        ids = {}
        async with r.db.conn() as conn:
            for name in ("alpha", "영상팀"):
                ids[name] = await conn.fetchval("INSERT INTO worker_groups(name) VALUES ($1) RETURNING id", name)
                await conn.execute("INSERT INTO object_info_snapshots(group_id, data, node_count, source, fetched_at) "
                                   "VALUES ($1, $2, 1, 'test', now())", ids[name],
                                   gzip.compress(json.dumps({f"Node_{ids[name]}": {}}).encode()))
        me = {"X-ComfyFarm-User": "tester"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            async def nodes(group):
                return list((await c.get("/api/object_info", headers={**me, "X-ComfyFarm-Group": group})).json())
            assert await nodes(str(ids["영상팀"])) == [f"Node_{ids['영상팀']}"]
            assert await nodes("alpha") == [f"Node_{ids['alpha']}"]                    # older extensions
            assert await nodes("999999") == [f"Node_{ids['alpha']}"]                   # unknown: the first group
            assert await nodes("99999999999999999999999") == [f"Node_{ids['alpha']}"]  # beyond bigint: no 500


async def test_without_gpu_workers_the_ui_worker_stands_in_for_node_definitions(dsn, fakes):
    """A new farm: the router and the UI worker are up, no GPU worker yet. The screen must still open workflows and
    show the node packs — from the UI worker — and the group's own definitions take over once a GPU worker answers."""
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        ui = await fakes(nodes_extra=["UiSideNode"])
        me = {"X-ComfyFarm-User": "tester"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            async with r.db.conn() as conn:
                await conn.execute("INSERT INTO workers(name, base_url, kind) VALUES ('ui', $1, 'ui')", ui.url)
            async def online(n):
                for _ in range(100):
                    async with r.db.conn() as conn:
                        if await conn.fetchval("SELECT count(*) FROM workers WHERE state = 'online'") >= n:
                            return
                    await asyncio.sleep(0.1)
                raise AssertionError("workers did not come online")
            await online(1)
            oi = (await c.get("/api/object_info", headers=me)).json()          # no group at all yet
            assert "UiSideNode" in oi and "EmptyImage" in oi
            async with r.db.conn() as conn:
                await conn.execute("INSERT INTO worker_groups(name) VALUES ('g1')")
            assert "UiSideNode" in (await c.get("/api/object_info", headers=me)).json()   # a group, no GPU worker
            async with r.db.conn() as conn:
                assert (await conn.fetchval("SELECT source FROM object_info_snapshots")) == "ui:ui"
            gpu = await fakes(nodes_extra=["GpuOnlyNode"])
            await add_worker(r, gpu.url, name="gpu-1")
            await online(2)
            oi = (await c.get("/api/object_info", headers=me)).json()
            assert "GpuOnlyNode" in oi and "UiSideNode" not in oi                 # the group's own definitions
            async with r.db.conn() as conn:
                assert (await conn.fetchval("SELECT source FROM object_info_snapshots")) == "gpu-1"
            # its GPU workers away for now: the real snapshot stays, the UI worker does not overwrite it
            await gpu.stop()
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET state = 'offline' WHERE name = 'gpu-1'")
            gid = await _gid(r)
            assert await r.refresh_snapshot(gid) is None
            assert "GpuOnlyNode" in (await c.get("/api/object_info", headers=me)).json()


async def _gid(r):
    async with r.db.conn() as conn:
        return await conn.fetchval("SELECT id FROM worker_groups WHERE name = 'g1'")


async def test_node_definitions_that_could_not_be_read_are_read_again(dsn, fakes):
    """Field (dev.17): a rollout put two node packs on the group's worker and ended 5 s before the health monitor saw
    it back; the read after it found nobody online and was skipped without a word, and the editor kept the old
    definitions. A worker named by the caller is asked even before the monitor sees it; a read that finds nobody marks
    the snapshot stale, and the editor's next request reads it again once a worker answers."""
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        me = {"X-ComfyFarm-User": "tester"}
        old = await fakes(nodes_extra=["OldNode"])
        gid, wid = await add_worker(r, old.url, name="gpu-1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            for _ in range(100):
                async with r.db.conn() as conn:
                    if await conn.fetchval("SELECT state FROM workers WHERE id = $1", wid) == "online":
                        break
                await asyncio.sleep(0.1)
            assert "OldNode" in (await c.get("/api/object_info", headers=me)).json()
            for t in r.tasks:                                   # the health monitor's view is set by hand from here
                if t.get_coro().__qualname__ == "HealthMonitor.run":
                    t.cancel()
            new = await fakes(nodes_extra=["PackNode"])         # restarted with the new profile: two packs more
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET base_url = $2, state = 'offline' WHERE id = $1", wid, new.url)

            async def stale():
                async with r.db.conn() as conn:
                    return await conn.fetchval("SELECT stale FROM object_info_snapshots WHERE group_id = $1", gid)
            assert await r.refresh_snapshot(gid) is None and await stale()            # nobody online: marked
            oi = (await c.get("/api/object_info", headers=me)).json()
            assert "OldNode" in oi and "PackNode" not in oi                           # nobody to read it from yet
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET state = 'online' WHERE id = $1", wid)
            oi = (await c.get("/api/object_info", headers=me)).json()                # one answers: read again
            assert "PackNode" in oi and "OldNode" not in oi and not await stale()
            # what a rollout does now: the worker it just saw start is asked, whatever the monitor says yet
            newer = await fakes(nodes_extra=["NewerNode"])
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET base_url = $2, state = 'offline' WHERE id = $1", wid, newer.url)
            assert b"NewerNode" in await r.refresh_snapshot(gid, prefer=[wid])
            assert "NewerNode" in (await c.get("/api/object_info", headers=me)).json() and not await stale()


async def test_workers_in_maintenance_still_give_the_editor_its_node_definitions(dsn, fakes):
    """Field (dev.24): two failed rollouts left the group's only GPU worker (and the UI worker) in maintenance; the
    rollouts after them succeeded, but node definitions were read from workers in service only — nobody — and the
    editor kept those from before three node packs, without a word ("missing node pack" for a pack that was there).
    D-46: in maintenance a worker takes no jobs, but it runs the group's node sets: it is asked when no worker in
    service is there. Definitions served out of date carry the time they were read, for the editor to say so."""
    import gzip
    from comfyfarm.api.stock import STALE_HEADER
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        me = {"X-ComfyFarm-User": "tester"}
        old = await fakes(nodes_extra=["OldNode"])
        gid, wid = await add_worker(r, old.url, name="gpu-1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            for _ in range(100):
                async with r.db.conn() as conn:
                    if await conn.fetchval("SELECT state FROM workers WHERE id = $1", wid) == "online":
                        break
                await asyncio.sleep(0.1)
            res = await c.get("/api/object_info", headers=me)
            assert "OldNode" in res.json() and STALE_HEADER not in res.headers
            for t in r.tasks:                                   # the health monitor's view is set by hand from here
                if t.get_coro().__qualname__ == "HealthMonitor.run":
                    t.cancel()
            new = await fakes(nodes_extra=["PackNode"])         # its new profile: a pack more; and in maintenance
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET base_url = $2, maintenance = true WHERE id = $1", wid, new.url)
                await conn.execute("UPDATE object_info_snapshots SET stale = true WHERE group_id = $1", gid)
            res = await c.get("/api/object_info", headers=me)
            assert "PackNode" in res.json() and "OldNode" not in res.json() and STALE_HEADER not in res.headers
            async with r.db.conn() as conn:
                snap = await conn.fetchrow("SELECT source, stale FROM object_info_snapshots WHERE group_id = $1", gid)
            assert snap["source"] == "gpu-1" and not snap["stale"]

            # one in service and one in maintenance: the one in service is asked
            serving = await fakes(nodes_extra=["ServingNode"])
            _, wid2 = await add_worker(r, serving.url, name="gpu-2")
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET state = 'online' WHERE id = $1", wid2)
            assert b"ServingNode" in await r.refresh_snapshot(gid)
            assert b"PackNode" in await r.refresh_snapshot(gid, prefer=[wid])   # unless a rollout names the other

            # nobody answers: the old definitions, and when they were read
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET state = 'offline' WHERE group_id = $1", gid)
                await conn.execute("UPDATE object_info_snapshots SET stale = true WHERE group_id = $1", gid)
                read_at = await conn.fetchval("SELECT fetched_at FROM object_info_snapshots WHERE group_id = $1", gid)
            res = await c.get("/api/object_info", headers=me)
            assert "PackNode" in res.json() and res.headers[STALE_HEADER] == read_at.isoformat()
            res = await c.get("/api/object_info/PackNode", headers=me)
            assert list(res.json()) == ["PackNode"]
