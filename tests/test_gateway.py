"""Gateway end to end: router + gateway + UI-worker stub + fake GPU worker, all on real ports."""
import asyncio
import json
import uuid

import httpx
import pytest
import uvicorn
import websockets
from aiohttp import web

from comfyfarm.app import create_app
from comfyfarm.cli import add_user
from comfyfarm.gateway import GatewaySettings, create_gateway
from conftest import add_worker, fast_settings, free_port, tiny, wait_status

SECRET = "s3cret-for-tests"


async def serve_asgi(app, started):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                                           timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    started.append((server, task))
    return port


class UIStub:
    """Stands in for a stock --multi-user ComfyUI serving the frontend and user data; records what it was sent."""

    def __init__(self):
        self.users, self.seen = {}, []
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/users", self.get_users)
        self.app.router.add_post("/api/users", self.post_users)
        self.app.router.add_get("/api/settings", self.settings)
        self.app.router.add_get("/assets/index-AbCdEf12.js", self.asset)

    async def index(self, r):
        self.seen.append(dict(r.headers)); return web.Response(text="<html>stock frontend</html>", content_type="text/html")

    async def get_users(self, r):
        return web.json_response({"storage": "server", "users": self.users})

    async def post_users(self, r):
        name = (await r.json())["username"]
        uid = f"{name}_{uuid.uuid4()}"
        self.users[uid] = name
        return web.json_response(uid)

    async def settings(self, r):
        self.seen.append(dict(r.headers)); return web.json_response({"user": r.headers.get("comfy-user")})

    async def asset(self, r):
        return web.Response(text="js", headers={"Cache-Control": "no-store"})


@pytest.fixture
async def farm(dsn, fakes):
    started, runners = [], []
    router_app = create_app(fast_settings(dsn, dev_identity=None, gateway_secret=SECRET))
    router_port = await serve_asgi(router_app, started)
    ui = UIStub()
    runner = web.AppRunner(ui.app, shutdown_timeout=1); await runner.setup(); runners.append(runner)
    ui_port = free_port(); await web.TCPSite(runner, "127.0.0.1", ui_port).start()
    gw = create_gateway(GatewaySettings(f"http://127.0.0.1:{router_port}", f"http://127.0.0.1:{ui_port}", SECRET, b"k" * 32))
    gw_port = await serve_asgi(gw, started)
    worker = await fakes(ws_fix=True)
    await add_worker(router_app.state.router, worker.url)
    await add_user(dsn, "alice", "pw-alice", admin=False)
    await add_user(dsn, "root", "pw-root", admin=True)
    yield {"router": router_app.state.router, "router_url": f"http://127.0.0.1:{router_port}",
           "gw": f"http://127.0.0.1:{gw_port}", "ws": f"ws://127.0.0.1:{gw_port}/ws", "ui": ui, "worker": worker}
    for server, task in started:
        server.should_exit = True
        await task
    for r in runners:
        await r.cleanup()


async def login(c, gw, user, pw):
    return await c.post(f"{gw}/comfyfarm/login", data={"username": user, "password": pw})


async def test_login_routing_and_identity(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{gw}/")
        assert r.status_code == 303 and r.headers["location"] == "/comfyfarm/login"
        assert (await c.get(f"{gw}/api/queue")).status_code == 401
        assert (await login(c, gw, "alice", "wrong")).headers["location"].endswith("e=1")
        assert (await login(c, gw, "alice", "pw-alice")).status_code == 303
        assert (await c.get(f"{gw}/")).text == "<html>stock frontend</html>"
        # the browser cannot choose its identity: forged headers are replaced by the gateway
        s = (await c.get(f"{gw}/api/settings", headers={"comfy-user": "root_x", "x-comfyfarm-user": "root"})).json()
        assert s["user"] in farm["ui"].users and farm["ui"].users[s["user"]] == "alice"
        assert (await c.get(f"{gw}/api/users")).json() == {"storage": "server", "migrated": True}   # no user picker
        asset = await c.get(f"{gw}/assets/index-AbCdEf12.js")
        assert "immutable" in asset.headers["cache-control"]                  # stock sends no-store
        forged = await c.post(f"{gw}/api/prompt", json={"prompt": tiny(1)}, headers={"x-comfyfarm-user": "root"})
        pid = forged.json()["prompt_id"]
        await wait_status(farm["router"], pid, "completed")
        async with farm["router"].db.conn() as conn:
            assert await conn.fetchval("SELECT owner FROM jobs WHERE prompt_id = $1", uuid.UUID(pid)) == "alice"
        assert (await c.post(f"{gw}/api/admin/groups", json={"name": "x"})).status_code == 403   # alice is no admin


async def test_router_refuses_identity_headers_without_the_gateway(farm):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{farm['router_url']}/api/queue", headers={"x-comfyfarm-user": "alice"})
        assert r.status_code == 401
        r = await c.get(f"{farm['router_url']}/api/queue", headers={"x-comfyfarm-user": "alice", "x-comfyfarm-gateway": "guess"})
        assert r.status_code == 401
        r = await c.post(f"{farm['router_url']}/api/internal/login", json={"username": "alice", "password": "pw-alice"})
        assert r.status_code == 403


async def test_ws_through_gateway_and_temp_view_routing(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        cookie = f"comfyfarm_session={c.cookies['comfyfarm_session']}"
        router = farm["router"]
        for _ in range(50):
            await router.events.sync_workers()
            if all(st.connected for st, _ in router.events.streams.values()):
                break
            await asyncio.sleep(0.1)
        ws = await websockets.connect(f"{farm['ws']}?clientId=gwc1", additional_headers={"Cookie": cookie})
        first = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert first["type"] == "status"
        pid = (await c.post(f"{gw}/api/prompt", json={"prompt": tiny(2), "client_id": "gwc1"})).json()["prompt_id"]
        kinds = []
        while "execution_success" not in kinds:
            m = await asyncio.wait_for(ws.recv(), 5)
            kinds.append(json.loads(m)["type"] if isinstance(m, str) else "bin")
        assert "execution_start" in kinds and "bin" in kinds
        await ws.close()
        for _ in range(50):
            job = (await c.get(f"{gw}/api/jobs/{pid}")).json()
            if job.get("outputs"):
                break
            await asyncio.sleep(0.1)
        f = job["preview_output"]
        img = await c.get(f"{gw}/api/view", params={"filename": f["filename"], "subfolder": f["subfolder"], "type": "temp"})
        assert img.status_code == 200 and img.content.startswith(b"\x89PNG fake")   # served by the GPU worker
    async with httpx.AsyncClient() as c2:                                            # someone else cannot locate it
        await login(c2, gw, "root", "pw-root")
        other = await c2.get(f"{gw}/api/view", params={"filename": f["filename"], "subfolder": "", "type": "temp"})
        assert other.status_code != 200 or not other.content.startswith(b"\x89PNG fake")


async def test_gateway_owns_the_cross_site_check(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        assert (await c.get(f"{gw}/", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await c.post(f"{gw}/api/prompt", json={"prompt": tiny(9)}, headers={"Origin": "http://evil.example"})).status_code == 403
        same = gw.replace("http://", "")
        r = await c.get(f"{gw}/", headers={"Origin": f"http://{same}", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200                                       # and upstream did not see the Origin
        assert "origin" not in {k.lower() for k in farm["ui"].seen[-1]}


async def test_preview_available_when_executed_arrives(farm):
    """The stock frontend fetches /api/view as soon as it sees `executed`; the file must already be locatable."""
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        cookie = f"comfyfarm_session={c.cookies['comfyfarm_session']}"
        router = farm["router"]
        for _ in range(50):
            await router.events.sync_workers()
            if all(st.connected for st, _ in router.events.streams.values()):
                break
            await asyncio.sleep(0.1)
        ws = await websockets.connect(f"{farm['ws']}?clientId=gwc2", additional_headers={"Cookie": cookie})
        await asyncio.wait_for(ws.recv(), 3)
        await c.post(f"{gw}/api/prompt", json={"prompt": tiny(11), "client_id": "gwc2"})
        while True:
            m = await asyncio.wait_for(ws.recv(), 5)
            if isinstance(m, str) and json.loads(m)["type"] == "executed":
                img = json.loads(m)["data"]["output"]["images"][0]
                break
        r = await c.get(f"{gw}/api/view", params=img)                       # immediately, like the frontend
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG fake")
        await ws.close()


async def test_dashboard_through_gateway(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{gw}/comfyfarm/dashboard/")
        assert r.status_code == 303 and r.headers["location"] == "/comfyfarm/login?next=/comfyfarm/dashboard/"
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "root", "password": "pw-root", "next": "/comfyfarm/dashboard/"})
        assert r.headers["location"] == "/comfyfarm/dashboard/"
        page = await c.get(f"{gw}/comfyfarm/dashboard/")
        assert page.status_code == 200 and "app.js" in page.text           # served by the router, not the UI worker
        ov = (await c.get(f"{gw}/api/farm/overview?scope=all")).json()
        assert ov["me"] == {"user": "root", "admin": True} and ov["workers"][0]["base_url"]   # roles travel via the gateway
    async with httpx.AsyncClient() as c:
        # only fixed targets after login: anything else would be an open redirect
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "pw-alice", "next": "https://evil.example/"})
        assert r.headers["location"] == "/"
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "nope", "next": "//evil.example"})
        assert r.headers["location"] == "/comfyfarm/login?next=/&e=1"
        await login(c, gw, "alice", "pw-alice")
        ov = (await c.get(f"{gw}/api/farm/overview?scope=all")).json()
        assert ov["me"]["admin"] is False and "base_url" not in ov["workers"][0]
        assert (await c.patch(f"{gw}/api/admin/workers/1", json={"maintenance": True})).status_code == 403
