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
