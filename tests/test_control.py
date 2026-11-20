"""Holding dispatch, API tokens and comfyfarm-ctl, end to end through the gateway (docs/design/19)."""
import asyncio
import contextlib
import io
import json
import os
import stat
import uuid

import httpx
import pytest
import websockets

import comfyfarm.gateway as gateway_mod
from comfyfarm import ctl
from conftest import tiny, wait_status
from test_gateway import farm, login  # noqa: F401  (fixture)


@contextlib.asynccontextmanager
async def session(gw, user, pw):
    async with httpx.AsyncClient() as c:
        assert (await login(c, gw, user, pw)).status_code == 303
        yield c


async def make_token(c, gw, scope="user", name=None, days=90):
    r = await c.post(f"{gw}/api/farm/me/tokens", json={"name": name or f"t-{scope}", "scope": scope, "days": days})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tok):
    return {"Authorization": f"Bearer {tok['token'] if isinstance(tok, dict) else tok}"}


async def audit_rows(router, action):
    async with router.db.conn() as c:
        return [dict(r) for r in await c.fetch("SELECT actor, target, detail FROM audit_log WHERE action = $1 ORDER BY id", action)]


async def test_tokens_act_as_their_user_within_their_scope(farm, monkeypatch):
    gw, router = farm["gw"], farm["router"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice, \
            httpx.AsyncClient() as anon:
        admin_tok = await make_token(root, gw, "admin", "ci")
        assert admin_tok["token"].startswith("cft_") and admin_tok["hint"].endswith("…")
        listed = (await root.get(f"{gw}/api/farm/me/tokens")).json()["tokens"]
        assert [t["name"] for t in listed] == ["ci"] and "token" not in listed[0]          # shown once only
        assert (await alice.post(f"{gw}/api/farm/me/tokens", json={"name": "x", "scope": "admin"})).status_code == 403
        read_tok, user_tok = await make_token(alice, gw, "read"), await make_token(root, gw, "user")

        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(admin_tok))).json()["user"] == "root"
        assert (await anon.get(f"{gw}/api/admin/workers", headers=bearer(admin_tok))).status_code == 200
        # scope "user": the admin's own token without the admin role
        assert (await anon.get(f"{gw}/api/admin/workers", headers=bearer(user_tok))).status_code == 403
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(user_tok))).json()["admin"] is False
        # scope "read": GET only
        assert (await anon.get(f"{gw}/api/farm/queue", headers=bearer(read_tok))).status_code == 200
        r = await anon.post(f"{gw}/api/prompt", json={"prompt": tiny(1)}, headers=bearer(read_tok))
        assert r.status_code == 403 and "read-only" in r.text
        # a token cannot make or revoke tokens (a leaked one must not multiply)
        r = await anon.post(f"{gw}/api/farm/me/tokens", json={"name": "more", "scope": "user"}, headers=bearer(admin_tok))
        assert r.status_code == 403
        assert (await anon.delete(f"{gw}/api/farm/me/tokens/{user_tok['id']}", headers=bearer(admin_tok))).status_code == 403
        # the farm API only: not ComfyUI's routes on the UI worker, not the dashboard, not the event stream
        assert (await anon.get(f"{gw}/api/settings", headers=bearer(admin_tok))).status_code == 403
        assert (await anon.get(f"{gw}/comfyfarm/dashboard/", headers=bearer(admin_tok))).status_code == 403
        with pytest.raises(websockets.exceptions.InvalidStatus) as e:           # closed before accept: a 403
            async with websockets.connect(farm["ws"], additional_headers=bearer(admin_tok)) as ws:
                await ws.recv()
        assert e.value.response.status_code == 403
        # unknown and malformed tokens: a JSON 401, never the login redirect
        for h in (bearer("cft_" + "x" * 43), bearer("not-a-token"), {"Authorization": "Bearer"}):
            r = await anon.get(f"{gw}/api/farm/me", headers=h)
            assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
        assert not farm["ui"].seen or all("authorization" not in {k.lower() for k in h} for h in farm["ui"].seen)

        # the audit trail names the token; a browser cannot claim one
        assert (await anon.put(f"{gw}/api/admin/hold", json={"held": True, "reason": "t"}, headers=bearer(admin_tok))).status_code == 200
        assert (await root.put(f"{gw}/api/admin/hold", json={"held": False}, headers={"x-comfyfarm-token": "9:forged"})).status_code == 200
        held, released = await audit_rows(router, "dispatch held"), await audit_rows(router, "dispatch released")
        assert held[-1]["actor"] == "root" and held[-1]["detail"]["token"] == f"{admin_tok['id']}:ci"
        assert "token" not in released[-1]["detail"]

        # revocation, disabling the user, demotion and expiry all reach a token (cache off for the test)
        monkeypatch.setattr(gateway_mod, "SESSION_TTL", 0.0)
        assert (await root.delete(f"{gw}/api/farm/me/tokens/{user_tok['id']}")).status_code == 200
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(user_tok))).status_code == 401
        assert (await root.patch(f"{gw}/api/admin/users/alice", json={"disabled": True})).status_code == 200
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(read_tok))).status_code == 401
        assert (await root.patch(f"{gw}/api/admin/users/alice", json={"disabled": False})).status_code == 200
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(read_tok))).status_code == 200    # negative control
        async with router.db.conn() as c:
            await c.execute("UPDATE api_tokens SET expires_at = now() - interval '1 second' WHERE id = $1", read_tok["id"])
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(read_tok))).status_code == 401
        # an administrator sees and revokes everyone's tokens
        all_toks = (await root.get(f"{gw}/api/admin/tokens")).json()["tokens"]
        assert {t["user"] for t in all_toks} == {"root", "alice"}
        assert (await root.delete(f"{gw}/api/admin/tokens/{admin_tok['id']}")).status_code == 200
        assert (await anon.get(f"{gw}/api/farm/me", headers=bearer(admin_tok))).status_code == 401
        async with session(gw, "alice", "pw-alice") as alice2:    # disabling ended alice's sessions (D-35)
            assert (await alice2.get(f"{gw}/api/admin/tokens")).status_code == 403
        async with router.db.conn() as c:
            last = await c.fetchval("SELECT last_used_at FROM api_tokens WHERE id = $1", admin_tok["id"])
        assert last is not None


async def test_token_limits(farm):
    gw = farm["gw"]
    async with session(gw, "alice", "pw-alice") as alice:
        for body, code in (({"name": "", "scope": "user"}, 400), ({"name": "x", "scope": "god"}, 400),
                           ({"name": "x", "scope": "user", "days": 0}, 400), ({"name": "x", "scope": "user", "days": 366}, 400)):
            assert (await alice.post(f"{gw}/api/farm/me/tokens", json=body)).status_code == code, body
        for i in range(20):
            await make_token(alice, gw, name=f"n{i}")
        r = await alice.post(f"{gw}/api/farm/me/tokens", json={"name": "one too many", "scope": "user"})
        assert r.status_code == 409


async def test_hold_stops_dispatch_running_jobs_finish_and_release_starts_it(farm):
    gw, router = farm["gw"], farm["router"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice:
        # negative control: without a hold a job runs at once
        pid0 = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(1)})).json()["prompt_id"]
        await wait_status(router, pid0, "completed")
        slow = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(2, blur=25)})).json()["prompt_id"]   # ~4 s
        await wait_status(router, slow, "running")
        assert (await alice.put(f"{gw}/api/admin/hold", json={"held": True})).status_code == 403
        h = (await root.put(f"{gw}/api/admin/hold", json={"held": True, "reason": "driver update"})).json()
        assert h["held"] and h["by"] == "root" and h["reason"] == "driver update" and h["running"] == 1
        waiting = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(3)})).json()["prompt_id"]
        await wait_status(router, slow, "completed")                       # the running job finished
        await asyncio.sleep(1.5)                                           # several dispatch periods
        await wait_status(router, waiting, "pending", timeout=0.5)
        seen = (await alice.get(f"{gw}/api/farm/hold")).json()           # every user sees why their job waits
        assert seen["held"] and seen["pending"] == 1 and seen["running"] == 0
        health = (await root.get(f"{gw}/api/health/dispatch")).json()
        assert health["dispatch_held_since"] == h["since"]
        async with httpx.AsyncClient() as c:
            metrics = (await c.get(f"{farm['router_url']}/metrics")).text
        assert "\ncomfyfarm_dispatch_held_timestamp_seconds " in metrics
        # holding again keeps "since" (when the farm stopped) and changes the rest
        h2 = (await root.put(f"{gw}/api/admin/hold", json={"held": True, "reason": "longer", "minutes": 30})).json()
        assert h2["since"] == h["since"] and h2["reason"] == "longer" and h2["until"]
        r = (await root.put(f"{gw}/api/admin/hold", json={"held": False})).json()
        assert not r["held"]
        await wait_status(router, waiting, "completed")
        async with httpx.AsyncClient() as c:
            assert "\ncomfyfarm_dispatch_held_timestamp_seconds " not in (await c.get(f"{farm['router_url']}/metrics")).text
        assert (await root.put(f"{gw}/api/admin/hold", json={"held": True, "minutes": 0})).status_code == 400
        actions = [a for a in ("dispatch held", "dispatch hold changed", "dispatch released") if await audit_rows(router, a)]
        assert actions == ["dispatch held", "dispatch hold changed", "dispatch released"]


async def test_a_hold_ends_by_itself_at_until(farm):
    gw, router = farm["gw"], farm["router"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice:
        await root.put(f"{gw}/api/admin/hold", json={"held": True, "minutes": 5})
        pid = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(4)})).json()["prompt_id"]
        await asyncio.sleep(1.0)
        await wait_status(router, pid, "pending", timeout=0.5)
        async with router.db.conn() as c:                                   # five minutes later
            await c.execute("UPDATE settings SET value = jsonb_set(value, '{until}', to_jsonb((now() - interval '1 second')::text)) "
                            "WHERE key = 'dispatch_hold'")
        await wait_status(router, pid, "completed")
        expired = await audit_rows(router, "dispatch hold expired")
        assert len(expired) == 1 and expired[0]["actor"] == "comfyfarm"
        assert not (await alice.get(f"{gw}/api/farm/hold")).json()["held"]


async def test_admin_changes_and_job_operations_are_audited(farm):
    gw, router = farm["gw"], farm["router"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice:
        assert (await root.post(f"{gw}/api/admin/groups", json={"name": "g2"})).status_code == 200
        gid = next(g["id"] for g in (await root.get(f"{gw}/api/admin/groups")).json() if g["name"] == "g2")
        assert (await root.patch(f"{gw}/api/admin/groups/{gid}", json={"name": "g3"})).status_code == 200
        assert (await root.delete(f"{gw}/api/admin/groups/{gid}")).status_code == 200
        assert (await root.post(f"{gw}/api/admin/users", json={"username": "bob", "password": "pw-bob-123"})).status_code == 200
        assert (await root.patch(f"{gw}/api/admin/users/bob", json={"password": "pw-bob-456"})).status_code == 200
        assert (await root.delete(f"{gw}/api/admin/users/bob")).status_code == 200
        wid = (await root.post(f"{gw}/api/admin/workers", json={"name": "w9", "base_url": "http://127.0.0.1:9"})).json()["id"]
        assert (await root.delete(f"{gw}/api/admin/workers/{wid}")).status_code == 200
        await root.put(f"{gw}/api/admin/hold", json={"held": True})
        pid = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(5)})).json()["prompt_id"]
        assert (await alice.post(f"{gw}/api/farm/jobs/{pid}/pause")).status_code == 200
        assert (await alice.post(f"{gw}/api/farm/jobs/{pid}/resume")).status_code == 200
        assert (await root.post(f"{gw}/api/farm/jobs/{pid}/move", json={"to": "top"})).status_code == 200
        assert (await alice.post(f"{gw}/api/farm/jobs/{pid}/cancel")).status_code == 200
    for action, target in (("group created", "g2"), ("group changed", "g2"), ("group deleted", "g3"),
                           ("user created", "bob"), ("user changed", "bob"), ("user deleted", "bob"),
                           ("worker added", "w9"), ("worker deleted", "w9"), ("job paused", pid), ("job resumed", pid),
                           ("job moved", pid), ("job cancelled", pid)):
        rows = await audit_rows(router, action)
        assert rows and rows[-1]["target"] == target, action
    changed = (await audit_rows(router, "user changed"))[-1]["detail"]
    assert changed["password"] == "(set)" and "pw-bob" not in json.dumps(changed)        # never the password


# ---- comfyfarm-ctl --------------------------------------------------------------------------------------------------
async def run_ctl(*argv, token=None, url=None, stdin=None, config=None):
    """The CLI in a thread (it blocks on urllib), its exit code and stdout."""
    def go():
        import sys
        env = {"COMFYFARM_URL": url, "COMFYFARM_TOKEN": token}
        old = {k: os.environ.get(k) for k in env}
        buf, old_in = io.StringIO(), sys.stdin
        try:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            if stdin is not None:
                sys.stdin = io.StringIO(stdin)
            with contextlib.redirect_stdout(buf):
                rc = ctl.main([*argv, *(["--config", config] if config else [])])
        finally:
            sys.stdin = old_in
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return rc, buf.getvalue()
    return await asyncio.to_thread(go)


def last_json(text):
    return json.loads(text.strip().splitlines()[-1]) if text.strip().startswith("{") and "\n" not in text.strip() else json.loads(text)


async def test_ctl_end_to_end(farm, tmp_path):
    gw, router = farm["gw"], farm["router"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice:
        rt, at = (await make_token(root, gw, "admin", "agent"))["token"], (await make_token(alice, gw, "user"))["token"]
        pid_done = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(6)})).json()["prompt_id"]
        await wait_status(router, pid_done, "completed")
    cfg = str(tmp_path / "ctl.json")

    rc, o = await run_ctl("login", "--url", gw, "--json", stdin=rt + "\n", config=cfg)
    assert rc == 0 and json.loads(o)["user"] == "root"
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600 and json.load(open(cfg))["token"] == rt
    rc, o = await run_ctl("--json", "whoami", config=cfg)                       # options before the command work too
    assert rc == 0 and json.loads(o)["admin"] is True
    rc, o = await run_ctl("hold", "--reason", "agent maintenance", "--for", "2h", "--wait", "--json", config=cfg)
    h = json.loads(o)
    assert rc == 0 and h["held"] and h["reason"] == "agent maintenance" and h["until"] and h["running"] == 0
    rc, o = await run_ctl("status", config=cfg)
    assert rc == 0 and "DISPATCH HELD" in o and "agent maintenance" in o

    pid = None
    async with session(gw, "alice", "pw-alice") as alice:
        pid = (await alice.post(f"{gw}/api/prompt", json={"prompt": tiny(7)})).json()["prompt_id"]
    rc, o = await run_ctl("jobs", "list", "--all", "--json", config=cfg)
    assert rc == 0 and [j["prompt_id"] for j in json.loads(o)["jobs"]] == [pid]
    rc, o = await run_ctl("jobs", "pause", pid, "--json", token=at, url=gw)           # alice's own job, her token
    assert rc == 0 and json.loads(o)["results"][0]["ok"]
    rc, o = await run_ctl("jobs", "resume", pid, token=at, url=gw)
    assert rc == 0
    rc, o = await run_ctl("jobs", "pause", pid_done, "--json", token=at, url=gw)      # finished: conflict
    assert rc == ctl.CONFLICT and not json.loads(o)["results"][0]["ok"]
    rc, o = await run_ctl("jobs", "pause", str(uuid.uuid4()), token=at, url=gw)
    assert rc == ctl.NOT_FOUND
    rc, _ = await run_ctl("release", token=at, url=gw)                                # alice is no admin
    assert rc == ctl.AUTH
    rc, o = await run_ctl("jobs", "cancel", pid, "--json", config=cfg, stdin="")       # not a terminal, no --yes
    assert rc == ctl.USAGE
    rc, _ = await run_ctl("jobs", "cancel", pid, "--yes", config=cfg)
    assert rc == 0
    await wait_status(router, pid, "cancelled")
    rc, o = await run_ctl("release", "--json", config=cfg)
    assert rc == 0 and json.loads(o)["held"] is False
    rc, o = await run_ctl("workers", "list", "--json", config=cfg)
    assert rc == 0 and len(json.loads(o)) == 1
    rc, o = await run_ctl("whoami", "--json", token="cft_" + "y" * 43, url=gw)
    assert rc == ctl.AUTH and json.loads(o)["exit"] == ctl.AUTH
    rc, _ = await run_ctl("whoami", token=rt, url="http://127.0.0.1:9")                # nothing listens there
    assert rc == ctl.UNAVAILABLE
    rc, _ = await run_ctl("whoami", url=gw, config=str(tmp_path / "none.json"))
    assert rc == ctl.USAGE
    rc, o = await run_ctl("commands", "--json")
    names = {c["command"] for c in json.loads(o)["commands"]}
    assert {"hold", "release", "jobs pause", "jobs cancel", "workers list", "commands"} <= names
    hold_args = next(c for c in json.loads(o)["commands"] if c["command"] == "hold")["arguments"]
    assert {"--reason", "--for", "--wait"} <= {a["name"] for a in hold_args}
    rc, _ = await run_ctl("logout", config=cfg)
    assert rc == 0 and not os.path.exists(cfg)
    async with router.db.conn() as c:                    # what the agent did is in the audit log, under its token
        toks = [r["detail"].get("token") for r in await c.fetch(
            "SELECT detail FROM audit_log WHERE action IN ('dispatch held', 'job cancelled', 'dispatch released') ORDER BY id")]
    assert toks and all(t and t.endswith(":agent") for t in toks)


# ---- phase 2: workers, builds, node packs, rollouts -----------------------------------------------------------------
@pytest.fixture
async def farm_b(dsn, fakes, tmp_path):
    """The gateway farm with a control-plane builder directory (the builder itself is played by the test)."""
    from aiohttp import web
    from comfyfarm.app import create_app
    from comfyfarm.cli import add_user
    from comfyfarm.gateway import GatewaySettings, create_gateway
    from conftest import add_worker, fast_settings, free_port
    from test_gateway import SECRET, UIStub, serve_asgi
    builds = tmp_path / "builds"
    for d in ("requests", "status"):
        (builds / d).mkdir(parents=True)
    started = []
    app = create_app(fast_settings(dsn, dev_identity=None, gateway_secret=SECRET, builds_dir=str(builds)))
    rport = await serve_asgi(app, started)
    ui = UIStub()
    runner = web.AppRunner(ui.app, shutdown_timeout=1); await runner.setup()
    uport = free_port(); await web.TCPSite(runner, "127.0.0.1", uport).start()
    gw = create_gateway(GatewaySettings(f"http://127.0.0.1:{rport}", f"http://127.0.0.1:{uport}", SECRET, b"k" * 32))
    gport = await serve_asgi(gw, started)
    worker = await fakes(ws_fix=True)
    _, wid = await add_worker(app.state.router, worker.url, name="gpu-1")
    await add_user(dsn, "alice", "pw-alice", admin=False, groups=["g1"])
    await add_user(dsn, "root", "pw-root-1", admin=True)
    yield {"router": app.state.router, "gw": f"http://127.0.0.1:{gport}", "builds": builds, "wid": wid}
    for server, task in started:
        server.should_exit = True
        await task
    await runner.cleanup()


class PlayBuilder:
    """Answers the router's builder requests the way the control-plane builder does (status files), in the background."""

    def __init__(self, builds, commits=None, fail=()):
        self.builds, self.commits, self.fail, self.seen = builds, dict(commits or {}), set(fail), []
        from comfyfarm import builder
        self.builder = builder
        builder.write_json(str(builds / "status" / "policy.json"),
                           {"recipes": ["rocm-x"], "node_sources": ["https://github.com/"], "about": {"rocm-x": "AMD"}})
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            for f in sorted((self.builds / "requests").glob("*.json")):
                req, rid = json.loads(f.read_text()), f.stem
                f.unlink()
                self.seen.append(req)
                st = {"id": rid, "kind": req["kind"]}
                if req["kind"] == "resolve":
                    st.update(state="done", commit=self.commits[req["ref"] or "HEAD"]) if (req["ref"] or "HEAD") in self.commits \
                        else st.update(state="failed", error=f"no ref {req['ref']!r}")
                elif req["kind"] == "build":
                    st.update(recipe=req["recipe"], state="failed" if req["recipe"] in self.fail else "done",
                              template=f"{req['recipe']}-1", **({"error": "pip failed"} if req["recipe"] in self.fail else {}))
                self.builder.write_json(str(self.builds / "status" / f"{rid}.json"), st)
            await asyncio.sleep(0.1)

    def stop(self):
        self.task.cancel()


async def test_ctl_workers_builds_packs_and_rollouts(farm_b):
    gw, router, builds = farm_b["gw"], farm_b["router"], farm_b["builds"]
    async with session(gw, "root", "pw-root-1") as root, session(gw, "alice", "pw-alice") as alice:
        rt = (await make_token(root, gw, "admin", "ops-agent"))["token"]
        at = (await make_token(alice, gw, "user"))["token"]
    ctl_ = lambda *a, **k: run_ctl(*a, token=k.pop("token", rt), url=gw, **k)

    # workers: by name, several at once, unknown ones named, non-admins refused
    rc, o = await ctl_("workers", "maintenance", "on", "gpu-1", "--json")
    assert rc == 0 and json.loads(o)["results"][0]["ok"]
    async with router.db.conn() as c:
        assert await c.fetchval("SELECT maintenance FROM workers WHERE name = 'gpu-1'")
    rc, o = await ctl_("workers", "maintenance", "off", "gpu-1", "nope", "--json")
    assert rc == ctl.NOT_FOUND and [r["ok"] for r in json.loads(o)["results"]] == [True, False]
    async with router.db.conn() as c:
        assert not await c.fetchval("SELECT maintenance FROM workers WHERE name = 'gpu-1'")
    rc, _ = await ctl_("workers", "disable", str(farm_b["wid"]))                          # by id
    rc2, _ = await ctl_("workers", "enable", "gpu-1")
    assert rc == rc2 == 0
    rc, o = await ctl_("workers", "show", "gpu-1", "--json")
    assert rc == 0 and json.loads(o)["name"] == "gpu-1" and json.loads(o)["enabled"]
    rc, _ = await ctl_("workers", "maintenance", "on", "gpu-1", token=at)
    assert rc == ctl.AUTH
    rc, _ = await ctl_("workers", "reset", "gpu-1", stdin="")                             # not a terminal, no --yes
    assert rc == ctl.USAGE

    # builds: the builder down, then up; --wait follows it to the end and the exit code says how it went
    rc, _ = await ctl_("builds", "start", "rocm-x")
    assert rc == ctl.UNAVAILABLE                                                         # 503: no builder running
    pb = PlayBuilder(builds, commits={"main": "a" * 40, "HEAD": "b" * 40, "v2": "c" * 40}, fail={"rocm-bad"})
    try:
        rc, o = await ctl_("builds", "recipes", "--json")
        assert rc == 0 and json.loads(o)["recipes"] == ["rocm-x"]
        rc, o = await ctl_("builds", "start", "rocm-x", "--wait", "--json")
        b = json.loads(o)
        assert rc == 0 and b["state"] == "done" and b["template"] == "rocm-x-1"
        rc, o = await ctl_("builds", "start", "nope", "--json")
        assert rc == ctl.FAILED and "no recipe" in json.loads(o)["error"]               # 400 from the router
        pb.builder.write_json(str(builds / "status" / "policy.json"),
                              {"recipes": ["rocm-x", "rocm-bad"], "node_sources": ["https://github.com/"]})
        rc, o = await ctl_("builds", "start", "rocm-bad", "--wait", "--json")
        assert rc == ctl.FAILED and json.loads(o)["state"] == "failed"
        rc, o = await ctl_("builds", "show", b["id"], "--json")
        assert rc == 0 and json.loads(o)["state"] == "done"

        # node packs: pinned to the commit the builder looks up; update moves it; remove asks first
        rc, o = await ctl_("packs", "add", "https://github.com/someone/ComfyUI-Thing.git", "--ref", "main", "--gpu-only",
                           "--constraint", "kornia<0.8", "--json")
        assert rc == 0 and json.loads(o)["commit"] == "a" * 40 and json.loads(o)["name"] == "ComfyUI-Thing"
        rc, o = await ctl_("packs", "list", "--json")
        p = json.loads(o)[0]
        assert p["commit"] == "a" * 40 and p["ref"] == "main" and p["ui"] is False and p["constraints"] == ["kornia<0.8"]
        rc, o = await ctl_("packs", "update", "ComfyUI-Thing", "--json")
        assert rc == 0 and json.loads(o)["changed"] is False                              # main still names a…
        pb.commits["main"] = "d" * 40
        rc, o = await ctl_("packs", "update", "ComfyUI-Thing", "--json")
        assert rc == 0 and json.loads(o)["changed"] and json.loads(o)["commit"] == "d" * 40
        rc, o = await ctl_("packs", "add", "https://github.com/someone/Other", "--ref", "gone", "--json")
        assert rc == ctl.FAILED and "could not look up" in json.loads(o)["error"]
        rc, _ = await ctl_("packs", "remove", "ComfyUI-Thing", stdin="")
        assert rc == ctl.USAGE
        rc, _ = await ctl_("packs", "remove", "ComfyUI-Thing", "--yes")
        assert rc == 0
        rc, o = await ctl_("packs", "list", "--json")
        assert json.loads(o) == []
        assert {r["kind"] for r in pb.seen} == {"resolve", "build"}
    finally:
        pb.stop()

    # rollouts: a profile by name; waiting on one nobody drives ends at once with what to do
    rc, _ = await ctl_("rollouts", "start", "--profile", "nope", "--worker", "gpu-1", "--yes")
    assert rc == ctl.NOT_FOUND
    async with router.db.conn() as c:
        pid = await c.fetchval("INSERT INTO profiles(name, core, venv) VALUES ('p1', 'comfyui-x', 'venv-x') RETURNING id")
        rid = await c.fetchval("INSERT INTO rollouts(target_profile_id, status, created_by) VALUES ($1, 'interrupted', 'root') "
                               "RETURNING id", pid)
        await c.execute("INSERT INTO rollout_steps(rollout_id, worker_id, position, to_profile_id) VALUES ($1, $2, 0, $3)",
                        rid, farm_b["wid"], pid)
    rc, o = await ctl_("profiles", "list", "--json")
    assert rc == 0 and [p["name"] for p in json.loads(o)] == ["p1"]
    rc, _ = await ctl_("rollouts", "start", "--profile", "p1", "--yes")
    assert rc == ctl.USAGE                                                                 # no workers named
    rc, o = await ctl_("rollouts", "show", str(rid), "--wait", "--json")
    assert rc == ctl.FAILED and json.loads(o)["status"] == "interrupted"
    rc, o = await ctl_("rollouts", "start", "--profile", "p1", "--worker", "gpu-1", "--yes", "--json")
    assert rc in (ctl.CONFLICT, ctl.FAILED)                     # another rollout is active / the worker has no instance
    rc, o = await ctl_("rollouts", "abort", str(rid), "--yes", "--wait", "--json")
    assert rc == 0 and json.loads(o)["status"] == "aborted"                                # aborting is what was asked
    rc, o = await ctl_("rollouts", "abort", str(rid), "--yes", "--json")
    assert rc == ctl.CONFLICT
    rc, o = await ctl_("rollouts", "list", "--json")
    assert rc == 0 and json.loads(o)[0]["id"] == rid
    async with router.db.conn() as c:                        # every change above is in the audit log, under the token
        rows = [dict(r) for r in await c.fetch("SELECT action, detail FROM audit_log WHERE actor = 'root' AND action IN "
                                               "('worker changed', 'node pack added', 'node pack changed', 'node pack removed', "
                                               "'build requested', 'rollout abort')")]
    assert {r["action"] for r in rows} == {"worker changed", "node pack added", "node pack changed", "node pack removed",
                                           "build requested", "rollout abort"}
    assert all(r["detail"].get("token", "").endswith(":ops-agent") for r in rows)

