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
        self.refuse = False                                      # behave like a worker whose allow-list lacks the gateway
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/users", self.get_users)
        self.app.router.add_post("/api/users", self.post_users)
        self.app.router.add_get("/api/settings", self.settings)
        self.app.router.add_get("/assets/index-AbCdEf12.js", self.asset)
        self.app.router.add_get("/api/view", self.view)          # no shared storage in this test: nothing here
        self.views = []

    async def index(self, r):
        self.seen.append(dict(r.headers)); return web.Response(text="<html>stock frontend</html>", content_type="text/html")

    async def get_users(self, r):
        if self.refuse:
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response({"storage": "server", "users": self.users})

    async def post_users(self, r):
        name = (await r.json())["username"]
        uid = f"{name}_{uuid.uuid4()}"
        self.users[uid] = name
        return web.json_response(uid)

    async def settings(self, r):
        self.seen.append(dict(r.headers)); return web.json_response({"user": r.headers.get("comfy-user")})

    async def view(self, r):
        self.views.append(dict(r.query)); return web.json_response({"error": "no such file"}, status=404)

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
    await add_user(dsn, "alice", "pw-alice", admin=False, groups=["g1"])
    await add_user(dsn, "root", "pw-root-1", admin=True)
    yield {"router": router_app.state.router, "router_url": f"http://127.0.0.1:{router_port}",
           "gw": f"http://127.0.0.1:{gw_port}", "ws": f"ws://127.0.0.1:{gw_port}/ws", "ui": ui, "worker": worker,
           "servers": started}
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
        await login(c2, gw, "root", "pw-root-1")
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
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "root", "password": "pw-root-1", "next": "/comfyfarm/dashboard/"})
        assert r.headers["location"] == "/comfyfarm/dashboard/"
        page = await c.get(f"{gw}/comfyfarm/dashboard/")
        assert page.status_code == 200 and "app.js" in page.text           # served by the router, not the UI worker
        me = (await c.get(f"{gw}/api/farm/me")).json()
        assert me["user"] == "root" and me["admin"] is True                    # roles travel via the gateway
        assert (await c.get(f"{gw}/api/admin/workers")).json()[0]["base_url"]
    async with httpx.AsyncClient() as c:
        # only fixed targets after login: anything else would be an open redirect
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "pw-alice", "next": "https://evil.example/"})
        assert r.headers["location"] == "/comfyfarm/dashboard/"
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "nope", "next": "//evil.example"})
        assert r.headers["location"] == "/comfyfarm/login?next=/comfyfarm/dashboard/&e=1"
        await login(c, gw, "alice", "pw-alice")
        assert (await c.get(f"{gw}/api/farm/me")).json()["admin"] is False
        assert (await c.get(f"{gw}/api/admin/workers")).status_code == 403
        assert (await c.patch(f"{gw}/api/admin/workers/1", json={"maintenance": True})).status_code == 403


async def test_sessions_end_on_password_change_disable_and_demotion(farm, monkeypatch):
    """D-35: the cookie alone is not enough — the gateway re-confirms the account (cached SESSION_TTL; 0 here)."""
    import comfyfarm.gateway as gwmod
    monkeypatch.setattr(gwmod, "SESSION_TTL", 0.0)
    gw = farm["gw"]
    async with httpx.AsyncClient() as root, httpx.AsyncClient() as alice, httpx.AsyncClient() as alice2:
        await login(root, gw, "root", "pw-root-1")
        await login(alice, gw, "alice", "pw-alice")
        await login(alice2, gw, "alice", "pw-alice")                  # a second device
        assert (await alice.get(f"{gw}/api/farm/me")).json()["user"] == "alice"
        # a forged cookie role does not make an admin: roles come from the account
        assert (await alice.get(f"{gw}/api/admin/users")).status_code == 403

        r = await alice.post(f"{gw}/api/farm/me/password", json={"current": "pw-alice", "new": "pw-alice-2"})
        assert r.status_code == 200 and r.json()["relogin"]
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 401      # this browser…
        assert (await alice2.get(f"{gw}/api/farm/me")).status_code == 401     # …and every other one
        assert (await login(alice, gw, "alice", "pw-alice")).headers["location"].endswith("e=1")
        await login(alice, gw, "alice", "pw-alice-2")
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 200

        # promotion and demotion apply to running sessions
        assert (await root.patch(f"{gw}/api/admin/users/alice", json={"admin": True})).status_code == 200
        assert (await alice.get(f"{gw}/api/farm/me")).json()["admin"] is True
        assert (await root.patch(f"{gw}/api/admin/users/alice", json={"admin": False})).status_code == 200
        assert (await alice.get(f"{gw}/api/admin/users")).status_code == 403

        # disabling ends the session and blocks login
        assert (await root.patch(f"{gw}/api/admin/users/alice", json={"disabled": True})).status_code == 200
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 401
        assert (await alice.get(f"{gw}/")).status_code == 303                  # pages bounce to the login form
        assert (await login(alice, gw, "alice", "pw-alice-2")).headers["location"].endswith("e=1")


async def test_session_cache_window_and_router_outage(farm, monkeypatch):
    """Within SESSION_TTL a confirmed session is reused (that is the revocation delay); when the router cannot
    confirm a session, the gateway refuses with 503 instead of letting it through unconfirmed."""
    import comfyfarm.gateway as gwmod
    gw = farm["gw"]
    async with httpx.AsyncClient() as root, httpx.AsyncClient() as alice:
        await login(root, gw, "root", "pw-root-1")
        await login(alice, gw, "alice", "pw-alice")
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 200    # confirmed and cached
        await root.patch(f"{gw}/api/admin/users/alice", json={"disabled": True})
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 200    # still inside the cache window
        monkeypatch.setattr(gwmod, "SESSION_TTL", 0.0)
        assert (await alice.get(f"{gw}/api/farm/me")).status_code == 401    # window over
        server, task = farm["servers"][0]                                   # stop the router
        server.should_exit = True
        await task
        r = await root.get(f"{gw}/")
        assert r.status_code == 503 and "router unavailable" in r.text


async def test_login_lands_on_the_dashboard_and_optional_user_list(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        assert (await login(c, gw, "alice", "pw-alice")).headers["location"] == "/comfyfarm/dashboard/"   # default landing
        assert (await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "pw-alice", "next": "/"})).headers["location"] == "/"
        assert (await c.put(f"{gw}/api/admin/settings", json={"login_user_list": True})).status_code == 403   # admins only
    page = (await httpx.AsyncClient().get(f"{gw}/comfyfarm/login")).text
    assert "<input name=username" in page and "<select" not in page                     # off by default: no names
    assert "window.top !== window" in page                                             # breaks out of the Workflow iframe
    async with httpx.AsyncClient() as root:
        await login(root, gw, "root", "pw-root-1")
        assert (await root.put(f"{gw}/api/admin/settings", json={"login_user_list": "yes"})).status_code == 400
        assert (await root.put(f"{gw}/api/admin/settings", json={"nope": True})).status_code == 400
        assert (await root.put(f"{gw}/api/admin/settings", json={"login_user_list": True})).status_code == 200
        assert (await root.get(f"{gw}/api/admin/settings")).json()["login_user_list"]["value"] is True
        await add_user_disabled(farm, "mallory")
    page = (await httpx.AsyncClient().get(f"{gw}/comfyfarm/login")).text
    assert "<select name=username" in page and "<option>alice</option>" in page and "<option>root</option>" in page
    assert "mallory" not in page                                                       # disabled accounts are not offered
    async with httpx.AsyncClient() as c:                                               # the list does not replace the password
        assert (await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "x"})).headers["location"].endswith("e=1")


async def add_user_disabled(farm, name):
    async with farm["router"].db.conn() as c:
        await c.execute("INSERT INTO users(username, password_hash, disabled_at) VALUES ($1, 'scrypt$1$x$y', now())", name)


def saving(seed):
    return {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": seed}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "e2e"}}}


async def test_saved_output_falls_back_to_the_executing_worker(farm):
    """01: outputs normally come from shared storage via the UI worker; when the UI worker has no such file, the
    gateway asks the ledger which worker made it (owner-scoped) and fetches it there."""
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        pid = (await c.post(f"{gw}/api/prompt", json={"prompt": saving(3)})).json()["prompt_id"]
        await wait_status(farm["router"], pid, "completed")
        for _ in range(50):
            job = (await c.get(f"{gw}/api/jobs/{pid}")).json()
            if job.get("outputs"):
                break
            await asyncio.sleep(0.1)
        f = job["preview_output"]
        assert f["type"] == "output"
        r = await c.get(f"{gw}/api/view", params=f)
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG fake")          # from the GPU worker
        assert farm["ui"].views and farm["ui"].views[-1]["filename"] == f["filename"]    # the UI worker was asked first
        assert (await c.get(f"{gw}/api/view", params={**f, "filename": "nope.png"})).status_code == 404
    async with httpx.AsyncClient() as other:                                           # not the owner: nothing
        await login(other, gw, "root", "pw-root-1")
        assert (await other.get(f"{gw}/api/view", params=f)).status_code == 404


async def test_ui_worker_refusing_the_gateway_is_named_not_a_500(farm):
    gw = farm["gw"]
    farm["ui"].refuse = True
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        r = await c.get(f"{gw}/")
        assert r.status_code == 502 and "COMFYFARM_ALLOW" in r.json()["error"]
        farm["ui"].refuse = False                               # fixed on the worker: no restart of the gateway needed
        assert (await c.get(f"{gw}/")).text == "<html>stock frontend</html>"


async def raw_request(gw: str, line: str, cookie: str = "", extra: str = "") -> tuple[int, bytes]:
    """httpx normalises `..` before sending; a browser-made or hand-made request does not have to."""
    host, port = gw.removeprefix("http://").split(":")
    r, w = await asyncio.open_connection(host, int(port))
    w.write(f"{line} HTTP/1.1\r\nHost: {host}:{port}\r\nCookie: {cookie}\r\nContent-Length: 2\r\n{extra}"
            f"Connection: close\r\n\r\n{{}}".encode())
    await w.drain()
    data = await r.read()
    w.close()
    return int(data.split(b" ", 2)[1]), data


async def test_dot_segments_cannot_reroute(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        cookie = f"comfyfarm_session={c.cookies['comfyfarm_session']}"
    before = len(farm["ui"].seen)
    for path in ("/x/../prompt", "/x/%2e%2e/api/prompt", "/api/farm/../worker_profile", "/x/./queue", "//api/users"):
        status, _ = await raw_request(gw, f"POST {path}", cookie)
        assert status == 400, path
    assert len(farm["ui"].seen) == before                                    # nothing reached the UI worker


async def test_view_refuses_repeated_file_parameters(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        r = await c.get(f"{gw}/api/view?filename=VICTIM.png&type=output&filename=MINE.png")
        assert r.status_code == 400


async def test_cross_site_page_load_is_let_through_but_not_writes(farm):
    gw = farm["gw"]
    nav = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    async with httpx.AsyncClient() as c:
        await login(c, gw, "alice", "pw-alice")
        assert (await c.get(f"{gw}/", headers=nav)).status_code == 200
        assert (await c.post(f"{gw}/api/prompt", json={}, headers=nav)).status_code == 403
        assert (await c.get(f"{gw}/api/queue", headers={**nav, "sec-fetch-mode": "cors"})).status_code == 403


async def test_bad_cookie_is_401_not_500(farm):
    status, _ = await raw_request(farm["gw"], "GET /api/farm/me", "comfyfarm_session=a.\xe9")
    assert status == 401


async def test_open_websocket_ends_with_the_session(farm, monkeypatch):
    import comfyfarm.gateway as gwmod
    monkeypatch.setattr(gwmod, "SESSION_TTL", 0.3)
    gw = farm["gw"]
    async with httpx.AsyncClient() as root, httpx.AsyncClient() as alice:
        await login(root, gw, "root", "pw-root-1")
        await login(alice, gw, "alice", "pw-alice")
        async with websockets.connect(farm["ws"] + "?clientId=c1",
                                      additional_headers={"Cookie": f"comfyfarm_session={alice.cookies['comfyfarm_session']}"}) as ws:
            await root.patch(f"{gw}/api/admin/users/alice", json={"disabled": True})
            with pytest.raises(websockets.ConnectionClosed) as e:
                while True:
                    await asyncio.wait_for(ws.recv(), 5)
            assert e.value.rcvd.code == 4401


async def test_router_outage_at_login_is_a_message_not_a_500(farm):
    gw = farm["gw"]
    server, task = farm["servers"][0]
    server.should_exit = True
    await task
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{gw}/comfyfarm/login", data={"username": "alice", "password": "pw-alice"})
        assert r.status_code == 303 and r.headers["location"].endswith("e=2")
        assert "not answering" in (await c.get(f"{gw}{r.headers['location']}")).text


def test_dev_provider_refuses_a_public_address(monkeypatch):
    from comfyfarm.gateway import main
    monkeypatch.setenv("COMFYFARM_GATEWAY_SECRET", "s")
    monkeypatch.setenv("COMFYFARM_SESSION_KEY", "k" * 32)
    with pytest.raises(SystemExit):
        main(["--router", "http://r", "--ui", "http://u", "--provider", "dev", "--host", "0.0.0.0"])


async def test_login_page_speaks_the_chosen_language(farm):
    gw = farm["gw"]
    async with httpx.AsyncClient() as c:
        assert ">Log in<" in (await c.get(f"{gw}/comfyfarm/login")).text
        assert ">로그인<" in (await c.get(f"{gw}/comfyfarm/login", headers={"accept-language": "ko-KR,ko;q=0.9"})).text
        c.cookies.set("comfyfarm_lang", "en")                     # the dashboard setting beats the browser
        page = (await c.get(f"{gw}/comfyfarm/login?e=1", headers={"accept-language": "ko"})).text
        assert ">Log in<" in page and "Login failed" in page


async def test_ui_worker_auto_is_the_one_set_up_in_the_dashboard(dsn, fakes):
    """--ui auto: the gateway asks the router for the UI worker (kind ui), so setting one up needs no gateway change."""
    started, runners = [], []
    router_app = create_app(fast_settings(dsn, dev_identity=None, gateway_secret=SECRET))
    router_port = await serve_asgi(router_app, started)
    ui = UIStub()
    runner = web.AppRunner(ui.app, shutdown_timeout=1); await runner.setup(); runners.append(runner)
    ui_port = free_port(); await web.TCPSite(runner, "127.0.0.1", ui_port).start()
    gw = f"http://127.0.0.1:{await serve_asgi(create_gateway(GatewaySettings(f'http://127.0.0.1:{router_port}', 'auto', SECRET, b'k' * 32)), started)}"
    await add_user(dsn, "alice", "pw-alice", admin=False)
    try:
        async with httpx.AsyncClient() as c:
            await login(c, gw, "alice", "pw-alice")
            r = await c.get(f"{gw}/")
            assert r.status_code == 502 and "no UI worker is set up yet" in r.json()["error"]
            async with router_app.state.router.db.conn() as x:
                await x.execute("INSERT INTO workers(name, base_url, kind) VALUES ('ui-1', $1, 'ui')", f"http://127.0.0.1:{ui_port}")
            assert (await c.get(f"{gw}/")).text == "<html>stock frontend</html>"
    finally:
        for server, task in started:
            server.should_exit = True
            await task
        for r in runners:
            await r.cleanup()


async def test_first_administrator_from_the_login_page(dsn, caplog):
    """Easy deployment: no command line for the first administrator — the setup code from the router's log."""
    import logging
    started = []
    router_app = create_app(fast_settings(dsn, dev_identity=None, gateway_secret=SECRET))
    with caplog.at_level(logging.WARNING):
        router_port = await serve_asgi(router_app, started)
    code = router_app.state.router.setup_code
    assert code and code in caplog.text                                   # where the operator finds it
    gw = f"http://127.0.0.1:{await serve_asgi(create_gateway(GatewaySettings(f'http://127.0.0.1:{router_port}', 'auto', SECRET, b'k' * 32)), started)}"
    try:
        async with httpx.AsyncClient() as c:
            assert "action=/comfyfarm/setup" in (await c.get(f"{gw}/comfyfarm/login")).text

            async def setup(**kw):
                form = {"code": code, "username": "boss", "password": "password-1", "again": "password-1", **kw}
                r = await c.post(f"{gw}/comfyfarm/setup", data=form)
                return r.headers["location"] if r.status_code == 303 else r.text
            # a mistake brings the form back with the code and the name as typed (QA: both were gone after each one)
            page = await setup(code="AAAA-BBBB-CCCC")
            assert "Wrong setup code" in page and 'value="AAAA-BBBB-CCCC"' in page and 'value="boss"' in page
            page = await setup(again="password-2")
            assert "The passwords differ" in page and f'value="{code}"' in page and "password-1" not in page
            assert "at least 8 characters" in await setup(password="short", again="short")
            page = await setup(username='-x"><b id=injected>')
            assert "User name:" in page and "<b id=injected>" not in page and 'value="-x&quot;&gt;&lt;b id=injected&gt;"' in page
            assert await setup(code=f" {code.lower()} ") == "/comfyfarm/dashboard/"     # typed from a log: case, spaces
            me = (await c.get(f"{gw}/api/farm/me")).json()
            assert me["user"] == "boss" and me["admin"]
        async with httpx.AsyncClient() as other:                              # the offer is gone, the code is spent
            assert "action=/comfyfarm/login" in (await other.get(f"{gw}/comfyfarm/login")).text
            r = await other.post(f"{gw}/comfyfarm/setup", data={"code": code, "username": "eve", "password": "password-1",
                                                                "again": "password-1"})
            assert r.headers["location"] == "/comfyfarm/login"
        async with router_app.state.router.db.conn() as conn:
            assert await conn.fetchval("SELECT count(*) FROM users") == 1
            assert await conn.fetchval("SELECT count(*) FROM audit_log WHERE action = 'first administrator created'") == 1
        assert await router_app.state.router.first_admin_code() is None     # a restart offers nothing either
    finally:
        for server, task in started:
            server.should_exit = True
            await task


async def test_login_page_says_when_the_router_is_down_instead_of_an_ordinary_form(dsn):
    """Right after `docker compose up` the router may still be starting: the page must not pretend a login works."""
    from conftest import free_port
    started = []
    gw = f"http://127.0.0.1:{await serve_asgi(create_gateway(GatewaySettings(f'http://127.0.0.1:{free_port()}', 'auto', SECRET, b'k' * 32)), started)}"
    try:
        async with httpx.AsyncClient() as c:
            page = (await c.get(f"{gw}/comfyfarm/login")).text
            assert "not answering" in page and "action=/comfyfarm/login" in page
    finally:
        for server, task in started:
            server.should_exit = True
            await task


async def test_first_administrator_only_once_when_two_try_at_once(dsn):
    app = create_app(fast_settings(dsn, gateway_secret=SECRET))
    G = {"x-comfyfarm-gateway-login": SECRET}
    async with app.router.lifespan_context(app):
        code = app.state.router.setup_code
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            assert (await c.get("/api/internal/first-admin")).status_code == 403              # the gateway only
            assert (await c.get("/api/internal/first-admin", headers=G)).json() == {"needed": True}
            rs = await asyncio.gather(*(c.post("/api/internal/first-admin", headers=G, json={
                "code": code, "username": f"admin{i}", "password": "password-1"}) for i in range(4)))
            assert sorted(r.status_code for r in rs) == [200, 409, 409, 409]
            assert (await c.get("/api/internal/first-admin", headers=G)).json() == {"needed": False}
