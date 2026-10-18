"""M2 event relay through a real HTTP server (uvicorn) and real WebSocket clients (docs/design/05-events.md)."""
import asyncio
import json
import struct
import uuid

import httpx
import pytest
import uvicorn
import websockets

from comfyfarm.app import create_app
from conftest import add_worker, fast_settings, free_port, tiny, wait_status


class Served:
    def __init__(self, app, port):
        self.app, self.port = app, port
        self.http = f"http://127.0.0.1:{port}"
        self.ws = f"ws://127.0.0.1:{port}/ws"


@pytest.fixture
async def served(dsn):
    started = []

    async def start(**kw):
        app = create_app(fast_settings(dsn, **kw))
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                                               timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        started.append((server, task))
        return Served(app, port)

    yield start
    for server, task in started:
        server.should_exit = True
        await task


async def collect(ws, seconds):
    out = []
    end = asyncio.get_running_loop().time() + seconds
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            return out
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=left)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            return out
        out.append(json.loads(m) if isinstance(m, str) else m)


async def stream_ready(router, timeout=5.0):
    """Jobs dispatched before the router's worker WebSocket is up have their events sent to nobody (P10)."""
    t = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t < timeout:
        await router.events.sync_workers()
        if router.events.streams and all(st.connected for st, _ in router.events.streams.values()):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("worker event stream did not connect")


def kinds(msgs):
    return [m["type"] if isinstance(m, dict) else f"bin{struct.unpack('>I', m[:4])[0]}" for m in msgs]


async def test_relay_reaches_owner_only_and_previews_are_downgraded(served, fakes):
    s = await served()
    w = await fakes(ws_fix=True)
    await add_worker(s.app.state.router, w.url)
    await stream_ready(s.app.state.router)
    a = await websockets.connect(f"{s.ws}?clientId=bA&user=tester")
    b = await websockets.connect(f"{s.ws}?clientId=bB&user=mallory")
    await a.send(json.dumps({"type": "feature_flags", "data": {}}))      # no preview metadata support
    await collect(a, 0.3); await collect(b, 0.3)
    async with httpx.AsyncClient() as c:
        pid = (await c.post(f"{s.http}/api/prompt", json={"prompt": tiny(1), "client_id": "bA"})).json()["prompt_id"]
    got_a, got_b = await asyncio.gather(collect(a, 4), collect(b, 4))
    ka = kinds(got_a)
    assert "execution_start" in ka and "execution_success" in ka, ka
    assert "bin1" in ka and "bin4" not in ka, ka                          # downgraded for a non-metadata browser
    assert all(m.get("type") == "status" for m in got_b if isinstance(m, dict)) and not any(isinstance(m, bytes) for m in got_b), kinds(got_b)
    job = await wait_status(s.app.state.router, pid, "completed")
    assert job["status"] == "completed"
    await a.close(); await b.close()


async def test_metadata_browser_gets_type4(served, fakes):
    s = await served()
    w = await fakes(ws_fix=True)
    await add_worker(s.app.state.router, w.url)
    await stream_ready(s.app.state.router)
    a = await websockets.connect(f"{s.ws}?clientId=bM&user=tester")
    await a.send(json.dumps({"type": "feature_flags", "data": {"supports_preview_metadata": True}}))
    await collect(a, 0.3)
    async with httpx.AsyncClient() as c:
        await c.post(f"{s.http}/api/prompt", json={"prompt": tiny(2), "client_id": "bM"})
    assert "bin4" in kinds(await collect(a, 4))
    await a.close()


async def test_client_id_cannot_be_taken_over(served):
    s = await served()
    a = await websockets.connect(f"{s.ws}?clientId=bX&user=tester")
    await collect(a, 0.3)
    m = await websockets.connect(f"{s.ws}?clientId=bX&user=mallory")
    with pytest.raises(websockets.ConnectionClosed) as e:
        await asyncio.wait_for(m.recv(), 2)
    assert e.value.rcvd.code == 4403
    await a.close()


async def test_execution_error_arrives_via_ws_before_reconcile(served, fakes):
    s = await served(reconcile_period=60)                                  # only the WS path can finish it in time
    w = await fakes(ws_fix=True)
    w.fake.mode = "error"
    await add_worker(s.app.state.router, w.url)
    await stream_ready(s.app.state.router)
    a = await websockets.connect(f"{s.ws}?clientId=bE&user=tester")
    await collect(a, 0.3)
    async with httpx.AsyncClient() as c:
        pid = (await c.post(f"{s.http}/api/prompt", json={"prompt": tiny(3), "client_id": "bE"})).json()["prompt_id"]
    assert "execution_error" in kinds(await collect(a, 4))
    job = await wait_status(s.app.state.router, pid, "failed", timeout=5)
    assert job["error"]["kind"] == "execution" and job["error"]["message"] == "fake execution error"
    await a.close()


async def test_catch_up_after_reconnect(served, fakes):
    s = await served()
    w = await fakes(ws_fix=True)
    await add_worker(s.app.state.router, w.url)
    async with httpx.AsyncClient() as c:                                   # browser bC is not connected
        pid = (await c.post(f"{s.http}/api/prompt", json={"prompt": tiny(4), "client_id": "bC"})).json()["prompt_id"]
    await wait_status(s.app.state.router, pid, "completed")
    a = await websockets.connect(f"{s.ws}?clientId=bC&user=tester")
    got = await collect(a, 1.0)
    done = [m for m in got if isinstance(m, dict) and m["type"] == "execution_success"]
    assert done and done[0]["data"]["prompt_id"] == pid and done[0]["data"]["catch_up"] is True
    await a.close()


class StubSocket:
    """A browser socket that records what the router sends it."""
    def __init__(self):
        self.got = []

    async def send_bytes(self, b):
        self.got.append(b)

    async def send_text(self, t):
        self.got.append(json.loads(t))


async def test_frames_without_prompt_id_never_follow_a_job_across_a_reconnect(dsn, fakes):
    """D-28: a type-1 preview (no metadata) or an event without prompt_id belongs to the job the worker is executing.
    After the worker WebSocket reconnects (the worker may have restarted and now runs another user's job) that is not
    known until the worker says so — such frames are dropped, never sent to the previous job's owner."""
    from comfyfarm.app import Router
    from comfyfarm.core import jobs as jobsvc
    from comfyfarm.events.hub import WorkerStream
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    w = await fakes(ws_fix=True)
    _, wid = await add_worker(r, w.url)
    alice, bob = StubSocket(), StubSocket()
    r.events.browsers.bind("bA", "alice", alice)
    r.events.browsers.bind("bB", "bob", bob)
    ja, _ = await jobsvc.submit(r.db, owner="alice", prompt=tiny(1), browser_client_id="bA")
    jb, _ = await jobsvc.submit(r.db, owner="bob", prompt=tiny(2), browser_client_id="bB")
    st = WorkerStream(r.events, wid, w.url)
    task = asyncio.create_task(st.run())
    preview = struct.pack(">I", 1) + struct.pack(">I", 1) + b"\xff\xd8jpeg\xff\xd9"

    async def wait_for(cond):
        for _ in range(100):
            if cond():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("condition not reached")
    try:
        await wait_for(lambda: st.connected and st.client_id in w.fake.sockets)
        await st.on_message(json.dumps({"type": "execution_start", "data": {"prompt_id": str(ja["prompt_id"])}}))
        await st.on_message(preview)
        await wait_for(lambda: preview in alice.got)                  # negative control: before the gap it is alice's
        old = w.fake.sockets[st.client_id]
        await old.close()                                             # the worker restarts; the router reconnects
        await wait_for(lambda: st.connected and w.fake.sockets.get(st.client_id) not in (None, old))
        alice.got.clear()
        await st.on_message(preview)                                  # bob's job is running now, unannounced
        await st.on_message(json.dumps({"type": "executing", "data": {"node": "3"}}))
        await asyncio.sleep(0.2)
        assert alice.got == [] and bob.got == []
        # the worker names the prompt again: frames follow it
        await st.on_message(json.dumps({"type": "executing", "data": {"node": "2", "prompt_id": str(jb["prompt_id"])}}))
        await st.on_message(preview)
        await wait_for(lambda: preview in bob.got)
        assert alice.got == []
    finally:
        task.cancel()
        await r.stop()
