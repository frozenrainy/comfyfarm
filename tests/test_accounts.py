"""Accounts, group permissions (D-34), sessions (D-35), admin pages' API, statistics, history search."""
import asyncio
import datetime as dt
import uuid

import httpx
import pytest

from comfyfarm.app import create_app
from conftest import add_worker, fast_settings, free_port, tiny, wait_status

H = lambda user: {"X-ComfyFarm-User": user}           # noqa: E731  (dev identity: "admin" is an admin)


@pytest.fixture
async def farm(dsn):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield app.state.router, c


async def ok(resp, code=200):
    assert resp.status_code == code, resp.text
    return resp.json() if resp.content else None


async def group(c, name, **req):
    return (await ok(await c.post("/api/admin/groups", json={"name": name, "requirements": req}, headers=H("admin"))))["id"]


async def user(c, name, groups=(), admin=False):
    await ok(await c.post("/api/admin/users", json={"username": name, "password": "password-1", "admin": admin,
                                                    "groups": list(groups)}, headers=H("admin")))


async def submit(c, who, seed, **kw):
    return await c.post("/api/prompt", json={"prompt": tiny(seed, blur=30), **kw}, headers=H(who))


# ---- group permissions ----------------------------------------------------------------------------------------
async def test_group_permissions_route_and_revoke(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            a, b = await fakes(ws_fix=True), await fakes(ws_fix=True)
            g1, _ = await add_worker(r, a.url, group="g1", name="a")
            g2, _ = await add_worker(r, b.url, group="g2", name="b")
            await user(c, "bob", [g1])
            await user(c, "carol")

            denied = await submit(c, "bob", 1, comfyfarm_group=g2)
            assert denied.status_code == 403 and denied.json()["error"]["type"] == "group_not_permitted"
            assert (await submit(c, "carol", 2)).status_code == 403                       # no group at all
            assert [g["name"] for g in (await c.get("/api/farm/groups", headers=H("bob"))).json()] == ["g1"]
            me = (await c.get("/api/farm/me", headers=H("bob"))).json()
            assert me["groups"] == [{"id": g1, "name": "g1"}] and not me["all_groups"] and not me["admin"]

            # ungrouped jobs of bob run only where bob may run them (worker b is idle the whole time)
            pids = [(await ok(await submit(c, "bob", 10 + i)))["prompt_id"] for i in range(3)]
            for p in pids:
                await wait_status(r, p, "completed")
            assert set(pids) <= set(a.fake.post_log) and not set(pids) & set(b.fake.post_log)

            # revoke while queued (worker a in maintenance keeps it queued): the job stays, nobody takes it, it says so
            wa = next(w["id"] for w in (await c.get("/api/admin/workers", headers=H("admin"))).json() if w["name"] == "a")
            await ok(await c.patch(f"/api/admin/workers/{wa}", json={"maintenance": True}, headers=H("admin")))
            p = (await ok(await submit(c, "bob", 20)))["prompt_id"]
            await ok(await c.patch("/api/admin/users/bob", json={"groups": []}, headers=H("admin")))
            await ok(await c.patch(f"/api/admin/workers/{wa}", json={"maintenance": False}, headers=H("admin")))
            await asyncio.sleep(1.5)
            assert p not in a.fake.post_log + b.fake.post_log
            q = (await c.get("/api/farm/dashboard", headers=H("bob"))).json()["queue"]
            assert q[0]["prompt_id"] == p and q[0]["runnable"] is False
            health = (await c.get("/api/health/dispatch")).json()
            assert health["groups"][str(g1)]["dispatch_idle_seconds"] is None        # not a stall: nobody may take it
            assert health["ungrouped_unsatisfiable_pending"] == 1

            # grant another group: the queued job follows the new permission
            await ok(await c.patch("/api/admin/users/bob", json={"groups": [g2]}, headers=H("admin")))
            await wait_status(r, p, "completed")
            assert p in b.fake.post_log


async def test_admin_jobs_unrestricted_and_dev_names_without_account(farm):
    r, c = farm
    g1 = await group(c, "g1")
    assert (await submit(c, "admin", 1, comfyfarm_group=g1)).status_code == 200
    assert (await submit(c, "tester", 2)).status_code == 200          # dev identity, no account: unrestricted
    async with r.db.conn() as conn:
        rows = await conn.fetch("SELECT owner, allowed_groups FROM jobs ORDER BY id")
    assert [(x["owner"], x["allowed_groups"]) for x in rows] == [("admin", None), ("tester", None)]


# ---- admin guards ---------------------------------------------------------------------------------------------
async def test_lockout_guards(farm):
    r, c = farm
    await user(c, "root", admin=True)
    await user(c, "dora")
    # "root" acting on itself through the dev identity: roles come from dev_admins, so act as "admin" on root
    assert (await c.patch("/api/admin/users/root", json={"admin": False}, headers=H("admin"))).status_code == 409   # last admin
    assert (await c.patch("/api/admin/users/root", json={"disabled": True}, headers=H("admin"))).status_code == 409
    assert (await c.delete("/api/admin/users/root", headers=H("admin"))).status_code == 409
    await user(c, "admin", admin=True)
    assert (await c.patch("/api/admin/users/admin", json={"admin": False}, headers=H("admin"))).status_code == 400  # self
    assert (await c.delete("/api/admin/users/admin", headers=H("admin"))).status_code == 400
    await ok(await c.patch("/api/admin/users/root", json={"admin": False}, headers=H("admin")))    # now allowed
    assert (await c.post("/api/admin/users", json={"username": "x y", "password": "password-1"}, headers=H("admin"))).status_code == 400
    assert (await c.post("/api/admin/users", json={"username": "eve", "password": "short"}, headers=H("admin"))).status_code == 400
    assert (await c.post("/api/admin/users", json={"username": "dora", "password": "password-1"}, headers=H("admin"))).status_code == 409
    assert (await c.get("/api/admin/users", headers=H("dora"))).status_code == 403
    users = {u["username"]: u for u in (await c.get("/api/admin/users", headers=H("admin"))).json()}
    assert users["dora"]["groups"] == [] and not users["dora"]["admin"] and users["dora"]["local"]


async def test_groups_and_workers_admin(farm):
    r, c = farm
    g = await group(c, "lab", backend=["rocm"])
    assert (await c.post("/api/admin/groups", json={"name": "lab"}, headers=H("admin"))).status_code == 409
    assert (await c.post("/api/admin/groups", json={"name": "x", "requirements": {"gpu": 1}}, headers=H("admin"))).status_code == 400
    url = f"http://127.0.0.1:{free_port()}"
    wid = (await ok(await c.post("/api/admin/workers", json={"name": "w1", "base_url": url, "group": "lab"}, headers=H("admin"))))["id"]
    assert (await c.post("/api/admin/workers", json={"name": "w2", "base_url": url + "/"}, headers=H("admin"))).status_code == 409
    assert (await c.post("/api/admin/workers", json={"name": "w3", "base_url": "ftp://x"}, headers=H("admin"))).status_code == 400
    assert (await c.delete(f"/api/admin/groups/{g}", headers=H("admin"))).status_code == 409           # has a worker
    await ok(await c.patch(f"/api/admin/workers/{wid}", json={"group": ""}, headers=H("admin")))       # → no group
    assert (await c.get("/api/admin/workers", headers=H("admin"))).json()[0]["group"] is None
    await ok(await c.patch(f"/api/admin/groups/{g}", json={"name": "lab2", "requirements": {}}, headers=H("admin")))
    j = (await ok(await submit(c, "admin", 1, comfyfarm_group=g)))["prompt_id"]
    assert (await c.delete(f"/api/admin/groups/{g}", headers=H("admin"))).status_code == 409           # unfinished job
    await c.post(f"/api/farm/jobs/{j}/cancel", headers=H("admin"))
    await ok(await c.delete(f"/api/admin/groups/{g}", headers=H("admin")))
    # a deleted worker's URL and name can be registered again
    await ok(await c.delete(f"/api/admin/workers/{wid}", headers=H("admin")))
    await ok(await c.post("/api/admin/workers", json={"name": "w1", "base_url": url}, headers=H("admin")))


async def test_prefs_and_password(farm):
    r, c = farm
    await user(c, "erin")
    assert (await c.get("/api/farm/me", headers=H("erin"))).json()["prefs"] == {"theme": "light"}
    assert (await c.put("/api/farm/me/prefs", json={"theme": "neon"}, headers=H("erin"))).status_code == 400
    await ok(await c.put("/api/farm/me/prefs", json={"theme": "dark"}, headers=H("erin")))
    assert (await c.get("/api/farm/me", headers=H("erin"))).json()["prefs"]["theme"] == "dark"
    await ok(await c.put("/api/farm/me/prefs", json={"theme": "auto"}, headers=H("tester")))   # no account yet: created
    bad = await c.post("/api/farm/me/password", json={"current": "nope-nope", "new": "password-2"}, headers=H("erin"))
    assert bad.status_code == 403
    assert (await c.post("/api/farm/me/password", json={"current": "password-1", "new": "short"}, headers=H("erin"))).status_code == 400
    async with r.db.conn() as conn:
        before = await conn.fetchval("SELECT session_epoch FROM users WHERE username = 'erin'")
    assert (await ok(await c.post("/api/farm/me/password", json={"current": "password-1", "new": "password-2"}, headers=H("erin"))))["relogin"]
    async with r.db.conn() as conn:
        assert await conn.fetchval("SELECT session_epoch FROM users WHERE username = 'erin'") == before + 1


# ---- statistics and history --------------------------------------------------------------------------------------
async def finished_job(r, owner, status, *, finished_ago_h, run_s, wait_s, worker_id=None, note=None, error=None):
    now = dt.datetime.now(dt.timezone.utc)
    fin = now - dt.timedelta(hours=finished_ago_h)
    start = fin - dt.timedelta(seconds=run_s)
    created = start - dt.timedelta(seconds=wait_s)
    async with r.db.conn() as c:
        return await c.fetchval(
            "INSERT INTO jobs(prompt_id, status, priority, owner, created_at, dispatched_at, started_at, finished_at, worker_id, note, error) "
            "VALUES ($1, $2, 0, $3, $4, $5, $5, $6, $7, $8, $9) RETURNING prompt_id",
            uuid.uuid4(), status, owner, created, start, fin, worker_id, note, error)


async def test_stats_periods_and_numbers(farm):
    r, c = farm
    _, wid = await add_worker(r, f"http://127.0.0.1:{free_port()}", group="g1", name="w1")
    await finished_job(r, "ann", "completed", finished_ago_h=2, run_s=100, wait_s=10, worker_id=wid)
    await finished_job(r, "ann", "failed", finished_ago_h=3, run_s=50, wait_s=0, worker_id=wid)
    await finished_job(r, "ben", "completed", finished_ago_h=30, run_s=300, wait_s=30, worker_id=wid)
    await finished_job(r, "ben", "completed", finished_ago_h=24 * 40, run_s=1, wait_s=1)     # outside 30 days
    day = (await ok(await c.get("/api/admin/stats?days=1&tz=Asia/Seoul", headers=H("admin"))))
    assert day["total"]["jobs"] == 2 and day["total"]["completed"] == 1 and day["total"]["failed"] == 1
    assert day["total"]["avg_run_seconds"] == pytest.approx(100, abs=1) and day["total"]["gpu_seconds"] == pytest.approx(150, abs=1)
    week = await ok(await c.get("/api/admin/stats?days=7", headers=H("admin")))
    assert week["total"]["jobs"] == 3 and {u["user"] for u in week["users"]} == {"ann", "ben"}
    assert len(week["daily"]) in (7, 8) and sum(d["completed"] for d in week["daily"]) == 2
    w = week["workers"][0]
    assert w["name"] == "w1" and 0 < w["utilization"] < 0.01
    # range limit: 30 days, custom dates included
    assert (await c.get("/api/admin/stats?days=31", headers=H("admin"))).status_code == 400
    today = dt.date.today()
    assert (await c.get(f"/api/admin/stats?start={today - dt.timedelta(days=30)}&end={today}", headers=H("admin"))).status_code == 400
    ok30 = await ok(await c.get(f"/api/admin/stats?start={today - dt.timedelta(days=29)}&end={today}", headers=H("admin")))
    assert ok30["total"]["jobs"] == 3
    assert (await c.get("/api/admin/stats?tz=Mars/Base", headers=H("admin"))).status_code == 400
    assert (await c.get("/api/admin/stats", headers=H("ann"))).status_code == 403
    mine = await ok(await c.get("/api/farm/stats?days=7", headers=H("ann")))
    assert mine["total"]["jobs"] == 2 and mine["users"] == []


async def test_history_search_filters_and_pages(farm):
    r, c = farm
    for i in range(7):
        await finished_job(r, "ann", "completed", finished_ago_h=i + 1, run_s=10, wait_s=1, note=f"shot {i}")
    await finished_job(r, "ann", "failed", finished_ago_h=0.5, run_s=5, wait_s=1, error={"message": "CUDA out of memory"})
    await finished_job(r, "ben", "completed", finished_ago_h=1, run_s=10, wait_s=1, note="shot of ben")
    h = await ok(await c.get("/api/farm/history?limit=3", headers=H("ann")))
    assert h["total"] == 8 and len(h["jobs"]) == 3 and h["jobs"][0]["status"] == "failed"     # newest first, own only
    page2 = await ok(await c.get("/api/farm/history?limit=3&offset=3", headers=H("ann")))
    assert not {j["key"] for j in h["jobs"]} & {j["key"] for j in page2["jobs"]}
    assert (await ok(await c.get("/api/farm/history?q=memory", headers=H("ann"))))["total"] == 1
    assert (await ok(await c.get("/api/farm/history?q=SHOT", headers=H("ann"))))["total"] == 7
    assert (await ok(await c.get("/api/farm/history?status=failed", headers=H("ann"))))["total"] == 1
    assert (await ok(await c.get("/api/farm/history?q=ben", headers=H("ann"))))["total"] == 0          # owners: admins only
    assert (await ok(await c.get("/api/farm/history?q=ben&scope=all", headers=H("admin"))))["total"] == 1
    pid = h["jobs"][0]["prompt_id"]
    assert (await ok(await c.get(f"/api/farm/history?q={pid[:8]}", headers=H("ann"))))["total"] == 1
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    assert (await ok(await c.get(f"/api/farm/history?start={old}&end={old}", headers=H("ann"))))["total"] == 0
    assert (await c.get("/api/farm/history?status=running", headers=H("ann"))).status_code == 400


async def test_queue_badge_summary(farm):
    r, c = farm
    await group(c, "g1")
    for i in range(3):
        await submit(c, "admin", 40 + i)
    assert (await ok(await c.get("/api/farm/summary", headers=H("admin")))) == {"queued": 3, "running": 0}
    assert (await ok(await c.get("/api/farm/summary", headers=H("tester")))) == {"queued": 0, "running": 0}


async def test_retry_refuses_like_submit_when_the_owner_may_not_run_it(farm):
    """A retried copy nobody may take would wait forever; /prompt answers 403 for the same permissions, so retry does."""
    from comfyfarm.core import jobs as jobsvc
    r, c = farm
    g1 = await group(c, "g1")
    g2 = await group(c, "g2")
    await user(c, "carol")                                                    # no group at all
    await user(c, "bob", [g2])                                                # may no longer use g1
    for owner, gid in (("carol", None), ("bob", g1)):
        job, _ = await jobsvc.submit(r.db, owner=owner, prompt=tiny(1), group_id=gid)
        async with r.db.conn() as conn:
            await conn.execute("UPDATE jobs SET status = 'failed', finished_at = now() WHERE id = $1", job["id"])
        res = await c.post(f"/api/farm/jobs/{job['prompt_id']}/retry", headers=H("admin"))
        assert res.status_code == 403, (owner, res.text)
    async with r.db.conn() as conn:
        assert await conn.fetchval("SELECT count(*) FROM jobs WHERE retry_of IS NOT NULL") == 0


async def test_revocation_during_validation_is_not_missed(farm):
    """Permissions read before the (seconds-long) validation must not be stamped on the job if they were revoked in
    between: the insert re-reads them in its own transaction, serialised with the re-stamp."""
    from comfyfarm.core import access
    r, c = farm
    g1, _ = await add_worker(r, f"http://127.0.0.1:{free_port()}", group="g1", name="w")
    async with r.db.conn() as conn:
        await conn.execute("UPDATE workers SET state = 'online' WHERE name = 'w'")
    await user(c, "bob", [g1])

    async def validate(base_url, prompt):                                     # an admin revokes g1 meanwhile
        async with r.db.conn() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM user_groups WHERE username = 'bob'")
                await access.restamp(conn, "bob")
        return "ok", {}
    r.adapter.validate = validate
    res = await submit(c, "bob", 1)
    assert res.status_code == 403 and res.json()["error"]["type"] == "group_not_permitted", res.text
    async with r.db.conn() as conn:
        assert await conn.fetchval("SELECT count(*) FROM jobs WHERE owner = 'bob'") == 0


async def test_a_submit_waits_for_a_restamp_in_progress(farm):
    """The race the re-read alone does not close: permissions read while a revocation is still uncommitted, job
    committed after restamp's UPDATE ran. The per-owner lock makes the submit wait and see the new permissions."""
    from comfyfarm.core import access
    r, c = farm
    g1 = await group(c, "g1")
    await user(c, "bob", [g1])
    async with r.db.conn() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("DELETE FROM user_groups WHERE username = 'bob'")
        await access.restamp(conn, "bob")
        pending = asyncio.create_task(submit(c, "bob", 2))
        await asyncio.sleep(0.5)
        assert not pending.done()
        await tx.commit()
    res = await pending
    assert res.status_code == 403, res.text
