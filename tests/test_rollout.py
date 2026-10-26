"""Rollouts (docs/design/07, contract §7) against fake hosts that restart like the real launcher does."""
import asyncio
import json
import os

import httpx
import pytest

from comfyfarm.app import create_app
from comfyfarm.rollout import render as rd
from comfyfarm.testing.fake_host import FakeHost, FakeHostBackend
from conftest import fast_settings, free_port, tiny, wait_status

H = {"X-ComfyFarm-User": "admin"}
FAST = {"observe_timeout": 5, "start_timeout": 6, "verify_timeout": 5}


def publish(art, kind, name):
    d = art / kind / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"contract": "comfy-upgrade/1", "kind": kind, "name": name}))
    (d / "READY").write_text("")


@pytest.fixture
async def farm(dsn, tmp_path):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        art = tmp_path / "art"
        for kind, name in [("core", "c1"), ("core", "c2"), ("venv", "v1"), ("venv", "v2")]:
            publish(art, kind, name)
        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm")
        hosts = {}
        gid = (await api.post("/api/admin/groups", json={"name": "g1"}, headers=H)).json()["id"]
        p1 = (await api.post("/api/admin/profiles", json={"name": "p1", "core": "c1", "venv": "v1"}, headers=H)).json()["id"]
        p2 = (await api.post("/api/admin/profiles", json={"name": "p2", "core": "c2", "venv": "v2",
                                                         "flags": ["--preview-method", "auto"]}, headers=H)).json()["id"]
        for i in (1, 2):
            name = f"w{i}"
            port = free_port()
            paths = {k: str(tmp_path / name / k) for k in rd.PATH_KEYS}
            paths["art"] = str(art)
            h = FakeHost(port, router=api, instance=name, art=paths["art"], local=paths["local"],
                         prefixes=[paths[k] for k in ("models", "input", "output", "user", "temp")], down_seconds=0.8)
            wid = (await api.post("/api/admin/workers", json={"name": name, "base_url": h.url, "group": "g1"}, headers=H)).json()["id"]
            ok = await api.put(f"/api/admin/instances/{wid}", headers=H, json={
                "instance": name, "hosts": ["127.0.0.1"], "port": port, "paths": paths, "restart_backend": "manual"})
            assert ok.status_code == 200, ok.text
            assert (await api.post(f"/api/admin/instances/{wid}/assign", json={"profile_id": p1}, headers=H)).status_code == 200
            h.argv = (await api.get(f"/api/admin/instances/{wid}/render", headers=H)).json()["argv"][1:]   # launched on p1
            await h.start()
            h.wid = wid
            hosts[name] = h
        r.rollout.make_backend = lambda _name, _cfg: FakeHostBackend(hosts)
        r.rollout.poll = 0.2
        for _ in range(50):                                     # health sees both online
            if all(w["state"] == "online" for w in (await api.get("/api/admin/workers", headers=H)).json()):
                break
            await asyncio.sleep(0.1)
        yield {"router": r, "api": api, "hosts": hosts, "p1": p1, "p2": p2, "gid": gid, "art": art}
        for h in hosts.values():
            if h.task:
                h.task.cancel()
            await h.stop()
        await api.aclose()


async def start(api, profile, workers, **kw):
    res = await api.post("/api/admin/rollouts", headers=H, json={"profile_id": profile, "worker_ids": workers,
                                                                 "options": {**FAST, **kw.pop("options", {})}, **kw})
    assert res.status_code == 200, res.text
    return res.json()["id"]


async def wait_rollout(api, rid, timeout=40.0):
    t = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t < timeout:
        ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
        if ro["status"] not in ("running", "aborting"):
            return ro
        await asyncio.sleep(0.2)
    raise AssertionError(f"rollout still {ro['status']}: {ro['steps']}")


async def instances(api):
    return {i["worker"]: i for i in (await api.get("/api/admin/instances", headers=H)).json()}


async def test_rollout_succeeds_one_at_a_time_idle_first(farm):
    api, hosts = farm["api"], farm["hosts"]
    w1, w2 = hosts["w1"], hosts["w2"]
    assert {i["badge"] for i in (await instances(api)).values()} == {"match"}
    # w1 busy with a long job: the idle w2 goes first (shortest remaining time)
    pid = (await api.post("/api/prompt", json={"prompt": tiny(1, blur=30), "comfyfarm_group": farm["gid"]}, headers=H)).json()["prompt_id"]
    await wait_status(farm["router"], pid, "running")
    busy = next(h for h in hosts.values() if h.worker and pid in h.worker.post_log)
    idle = w2 if busy is w1 else w1
    rid = await start(api, farm["p2"], [busy.wid, idle.wid])
    for _ in range(100):
        h = (await api.get("/api/health/dispatch")).json()["groups"][str(farm["gid"])]
        if h["rollout_started_at"]:
            break
        await asyncio.sleep(0.05)
    assert h["rollout_started_at"]                                         # alert suppression while rolling
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "succeeded", ro
    assert [s["status"] for s in ro["steps"]] == ["done", "done"]
    assert w1.restarts == w2.restarts == 1
    by_worker = {s["worker_id"]: s for s in ro["steps"]}
    assert by_worker[idle.wid]["finished_at"] <= by_worker[busy.wid]["started_at"]   # idle first, one at a time
    inst = await instances(api)
    assert {i["badge"] for i in inst.values()} == {"match"} and {i["profile"] for i in inst.values()} == {"p2"}
    assert {i["comfyui"] for i in inst.values()} == {"c2-fake"}                 # version re-read, not just argv
    assert not any(w["maintenance"] for w in (await api.get("/api/admin/workers", headers=H)).json())
    assert (await api.get("/api/health/dispatch")).json()["groups"][str(farm["gid"])]["rollout_started_at"] is None
    actions = [a["action"] for a in (await api.get("/api/admin/audit", headers=H)).json()]
    assert actions.count("assign") >= 2 and "rollout succeeded" in actions and "rollout started" in actions


def long_job(seed, blurs=3):
    """~4 s per ImageBlur in the fake (one uninterruptible step each): a job that outlasts a blind drain."""
    wf = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": seed}}}
    for i in range(blurs):
        wf[str(i + 2)] = {"class_type": "ImageBlur", "inputs": {"image": [str(i + 1), 0], "blur_radius": 30, "sigma": 1.0}}
    wf["99"] = {"class_type": "PreviewImage", "inputs": {"images": [str(blurs + 1), 0]}}
    return wf


async def test_the_editor_gets_the_new_node_definitions_before_health_sees_the_workers_back(farm):
    """Field (dev.17): the rollout ended 5 s before the health monitor saw its worker back; the read of the group's
    node definitions after it found nobody online, and the editor kept those from before two node packs. The read now
    asks the workers the rollout just saw start."""
    api, r = farm["api"], farm["router"]
    asked, real = [], r.refresh_snapshot

    async def as_in_the_field(gid, prefer=None):            # the monitor has not seen them back yet
        async with r.db.conn() as c:
            await c.execute("UPDATE workers SET state = 'offline' WHERE group_id = $1", gid)
        asked.append((gid, sorted(prefer or [])))
        return await real(gid, prefer=prefer)
    r.refresh_snapshot = as_in_the_field
    rid = await start(api, farm["p2"], [farm["hosts"]["w1"].wid])
    assert (await wait_rollout(api, rid))["status"] == "succeeded"
    assert asked == [(farm["gid"], [farm["hosts"]["w1"].wid])]
    async with r.db.conn() as c:
        snap = await c.fetchrow("SELECT source, stale FROM object_info_snapshots WHERE group_id = $1", farm["gid"])
    assert snap is not None and snap["source"] == "w1" and not snap["stale"]


async def test_restart_waits_for_the_running_job(farm):
    api, hosts = farm["api"], farm["hosts"]
    pid = (await api.post("/api/prompt", json={"prompt": long_job(3), "comfyfarm_group": farm["gid"]}, headers=H)).json()["prompt_id"]
    await wait_status(farm["router"], pid, "running")
    busy = next(h for h in hosts.values() if h.worker and pid in h.worker.post_log)
    rid = await start(api, farm["p2"], [busy.wid])
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "succeeded", ro
    await wait_status(farm["router"], pid, "completed")                     # not cut off by the restart
    async with farm["router"].db.conn() as c:
        finished = await c.fetchval("SELECT finished_at FROM jobs WHERE prompt_id = $1::uuid", pid)
        restarted = await c.fetchval("SELECT min(at) FROM audit_log WHERE action = 'assign' AND actor LIKE 'rollout%'")
    assert restarted >= finished


async def test_launcher_refusal_reverts_and_stops_the_rollout(farm):
    api, hosts = farm["api"], farm["hosts"]
    # published to the router's eyes (name only) but not on the workers' disk: the launcher refuses (exit 2)
    p3 = (await api.post("/api/admin/profiles", json={"name": "p3", "core": "c2", "venv": "v-missing"}, headers=H)).json()["id"]
    rid = await start(api, p3, [hosts["w1"].wid, hosts["w2"].wid])
    ro = await wait_rollout(api, rid)
    s1, s2 = ro["steps"]
    assert ro["status"] == "failed"
    assert s1["status"] == "reverted" and "refused" in s1["error"]
    assert s2["status"] == "skipped" and hosts["w2"].restarts == 0          # canary stop
    inst = await instances(api)
    assert inst["w1"]["profile"] == "p1" and inst["w1"]["badge"] == "match"  # back on the old version
    workers = {w["name"]: w for w in (await api.get("/api/admin/workers", headers=H)).json()}
    assert workers["w1"]["maintenance"] and not workers["w2"]["maintenance"]  # failed one stays out of service


@pytest.mark.parametrize("modes,expect,why", [
    (["noop", "noop"], "revert_failed", "not seen down"),
    (["broken"], "reverted", "object_info"),
    (["wrong_argv"], "reverted", "different argv"),
    (["never_up"], "reverted", "never came back"),
    (["node_import_failed"], "reverted",                                     # runs, a node is missing — and why
     "custom nodes failed to import: ComfyUI-Broken (libGL.so.1 is missing on the host — install libgl1)"),
])
async def test_failed_restarts_are_detected(farm, modes, expect, why):
    api, h = farm["api"], farm["hosts"]["w1"]
    h.modes = list(modes)
    rid = await start(api, farm["p2"], [h.wid])
    ro = await wait_rollout(api, rid)
    step = ro["steps"][0]
    assert ro["status"] == "failed" and step["status"] == expect, step
    assert why in step["error"]
    if expect == "reverted":
        assert (await instances(api))["w1"]["badge"] == "match" and (await instances(api))["w1"]["profile"] == "p1"


async def test_abort_while_draining(farm):
    api, h = farm["api"], farm["hosts"]["w1"]
    pid = (await api.post("/api/prompt", json={"prompt": tiny(2, blur=30), "comfyfarm_group": farm["gid"]}, headers=H)).json()["prompt_id"]
    await wait_status(farm["router"], pid, "running")
    worker_with_job = next(x for x in farm["hosts"].values() if pid in (x.worker.post_log if x.worker else []))
    rid = await start(api, farm["p2"], [worker_with_job.wid])
    for _ in range(50):
        if (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()["steps"][0]["status"] == "draining":
            break
        await asyncio.sleep(0.1)
    assert (await api.post(f"/api/admin/rollouts/{rid}/abort", headers=H)).json()["status"] == "aborting"
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "aborted" and ro["steps"][0]["status"] == "skipped"
    assert worker_with_job.restarts == 0
    assert not any(w["maintenance"] for w in (await api.get("/api/admin/workers", headers=H)).json())


async def test_router_restart_interrupts_then_resume_finishes(farm):
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    engine_task = next(t for t in r.tasks if "Engine.run" in repr(t.get_coro()))
    rid = await start(api, farm["p2"], [hosts["w1"].wid, hosts["w2"].wid])
    for _ in range(750):
        steps = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()["steps"]
        if steps[0]["status"] == "starting":                   # restart requested, worker coming back
            break
        await asyncio.sleep(0.02)
    assert steps[0]["status"] == "starting"
    engine_task.cancel()                                       # the router dies mid-step
    await asyncio.gather(engine_task, return_exceptions=True)
    r.tasks.append(asyncio.create_task(r.rollout.run()))      # …and comes back
    for _ in range(50):
        if (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()["status"] == "interrupted":
            break
        await asyncio.sleep(0.1)
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert ro["status"] == "interrupted"
    assert (await api.post("/api/admin/rollouts", headers=H, json={"profile_id": farm["p1"], "worker_ids": [hosts["w2"].wid]})).status_code == 409
    await asyncio.sleep(1.5)
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert ro["status"] == "interrupted"                        # nobody drives it…
    assert ro["steps"][0]["status"] == "starting"               # …not even a step task left over from before
    assert (await api.post(f"/api/admin/rollouts/{rid}/resume", headers=H)).status_code == 200
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "succeeded", ro["steps"]
    assert hosts["w1"].restarts == 1                            # already on the target: not restarted again
    assert {i["badge"] for i in (await instances(api)).values()} == {"match"}


async def test_abort_of_an_interrupted_rollout(farm):
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    async with r.db.conn() as c:                                 # as left behind by a crash mid-drain / mid-restart
        rid = await c.fetchval("INSERT INTO rollouts(target_profile_id, created_by, status) VALUES ($1, 'x', 'interrupted') RETURNING id", farm["p2"])
        await c.execute("UPDATE workers SET maintenance = true")
        await c.execute("INSERT INTO rollout_steps(rollout_id, worker_id, position, to_profile_id, status, was_maintenance) VALUES "
                        "($1, $2, 0, $4, 'draining', false), ($1, $3, 1, $4, 'starting', false)", rid, hosts["w1"].wid, hosts["w2"].wid, farm["p2"])
    assert (await api.post(f"/api/admin/rollouts/{rid}/abort", headers=H)).json()["status"] == "aborted"
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert [s["status"] for s in ro["steps"]] == ["skipped", "failed"] and "check this worker" in ro["steps"][1]["error"]
    m = {w["name"]: w["maintenance"] for w in (await api.get("/api/admin/workers", headers=H)).json()}
    assert m == {"w1": False, "w2": True}


async def test_profiles_instances_and_render_endpoint(farm):
    api, hosts, p1, p2 = farm["api"], farm["hosts"], farm["p1"], farm["p2"]
    # profiles in use are immutable except the note (D-36)
    assert (await api.patch(f"/api/admin/profiles/{p1}", json={"venv": "v2"}, headers=H)).status_code == 409
    assert (await api.patch(f"/api/admin/profiles/{p1}", json={"note": "current"}, headers=H)).status_code == 200
    assert (await api.patch(f"/api/admin/profiles/{p2}", json={"venv": "v1"}, headers=H)).status_code == 200      # unused
    assert (await api.delete(f"/api/admin/profiles/{p1}", headers=H)).status_code == 409
    # the launcher's refusals are refused at the door
    for bad in ({"flags": ["--enable-manager"]}, {"flags": ["--output-directory", "/tmp"]}, {"env": {"LD_PRELOAD": "x"}},
                {"venv": "../../bin"}):
        res = await api.post("/api/admin/profiles", json={"name": "bad", "core": "c1", "venv": "v1", **bad}, headers=H)
        assert res.status_code == 400, bad
    # a custom node's setting is not a profile key: the refusal says where it goes instead; an allowed key with a bad
    # value does not get that hint
    res = await api.post("/api/admin/profiles", json={"name": "bad", "core": "c1", "venv": "v1",
                                                      "env": {"VHS_STRICT_PATHS": "1"}}, headers=H)
    assert res.status_code == 400 and "custom-nodes.md" in res.json()["detail"]
    res = await api.post("/api/admin/profiles", json={"name": "bad", "core": "c1", "venv": "v1",
                                                      "env": {"HSA_TOOLS_LIB": "/opt/x.so"}}, headers=H)
    assert res.status_code == 400 and "custom-nodes.md" not in res.json()["detail"]
    # render: only the instance's host; nothing for unknown or unassigned instances
    assert (await api.get("/api/worker_profile", params={"instance": "w1"})).json()["rev"]
    wid = hosts["w1"].wid
    cfg = (await instances(api))["w1"]["instance"]
    await api.put(f"/api/admin/instances/{wid}", headers=H, json={**cfg, "hosts": ["10.9.9.9"], "restart_config": {}})
    assert (await api.get("/api/worker_profile", params={"instance": "w1"})).status_code == 403
    assert (await api.get("/api/worker_profile", params={"instance": "nope"})).status_code == 404
    # assignment without restart: the badge says so
    assert (await api.post(f"/api/admin/instances/{hosts['w2'].wid}/assign", json={"profile_id": p2}, headers=H)).status_code == 200
    assert (await instances(api))["w2"]["badge"] == "differs"
    # a rollout needs instance configuration on every worker
    plain = (await api.post("/api/admin/workers", json={"name": "plain", "base_url": f"http://127.0.0.1:{free_port()}"}, headers=H)).json()["id"]
    listed = (await api.get("/api/admin/instances", headers=H)).json()
    assert sorted(i["worker_id"] for i in listed) == sorted([hosts["w1"].wid, hosts["w2"].wid, plain])   # no NULL ids
    assert next(i for i in listed if i["worker_id"] == plain)["instance"] is None
    res = await api.post("/api/admin/rollouts", headers=H, json={"profile_id": p2, "worker_ids": [plain]})
    assert res.status_code == 400 and "instance" in res.text
    assert (await api.get("/api/admin/profiles", headers={"X-ComfyFarm-User": "bob"})).status_code == 403
    assert (await api.get("/api/admin/artifacts", headers=H)).json() == {"available": False}


async def test_artifact_checks_when_the_router_sees_the_store(dsn, tmp_path):
    """Contract §9: a venv for another platform or accelerator is refused; an unknown accelerator only warned about."""
    art = tmp_path / "art"
    for kind, name in [("core", "c1"), ("venv", "cu"), ("venv", "win"), ("venv", "plain")]:
        publish(art, kind, name)
    for name, extra in [("cu", {"accelerator": "cuda"}), ("win", {"platform": "windows", "accelerator": "rocm"})]:
        m = json.loads((art / "venv" / name / "manifest.json").read_text())
        (art / "venv" / name / "manifest.json").write_text(json.dumps({**m, **extra}))
    app = create_app(fast_settings(dsn, artifacts_dir=str(art)))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as api:
            wid = (await api.post("/api/admin/workers", json={"name": "r1", "base_url": f"http://127.0.0.1:{free_port()}"}, headers=H)).json()["id"]
            async with r.db.conn() as c:
                await c.execute("UPDATE workers SET capabilities = '{\"backend\": \"rocm\", \"os\": \"posix\"}' WHERE id = $1", wid)
            paths = {k: str(tmp_path / k) for k in rd.PATH_KEYS} | {"art": str(art)}
            await api.put(f"/api/admin/instances/{wid}", headers=H, json={"instance": "r1", "port": 8188, "paths": paths})
            ids = {}
            for v in ("cu", "win", "plain", "missing"):
                ids[v] = (await api.post("/api/admin/profiles", json={"name": f"p-{v}", "core": "c1", "venv": v}, headers=H)).json()["id"]
            async def assign(v):
                return await api.post(f"/api/admin/instances/{wid}/assign", json={"profile_id": ids[v]}, headers=H)
            assert "built for cuda" in (await assign("cu")).text
            assert "is for windows" in (await assign("win")).text
            assert "not published" in (await assign("missing")).text
            ok = await assign("plain")
            assert ok.status_code == 200 and "does not declare its accelerator" in ok.json()["warnings"][0]
            listing = (await api.get("/api/admin/artifacts", headers=H)).json()
            assert listing["available"] and {a["name"] for a in listing["artifacts"]["venv"]} == {"cu", "win", "plain"}


async def test_secrets_are_not_accepted_into_the_ledger(farm, tmp_path):
    """F8: restart backends store references (key path, token_file, token_env), never a token value."""
    api, w = farm["api"], farm["hosts"]["w1"]
    farm["router"].s.keys_dir = str(tmp_path)                   # token files must lie in the keys directory (QA-7)
    cfg = (await instances(api))["w1"]["instance"]
    base = {**cfg, "restart_backend": "http-agent"}
    bad = await api.put(f"/api/admin/instances/{w.wid}", headers=H, json={**base, "restart_config": {"url": "http://h:8399", "token": "s3cret"}})
    assert bad.status_code == 400 and "never the token value" in bad.text
    tok = tmp_path / "agent.token"
    tok.write_text("t" * 32)
    ok = await api.put(f"/api/admin/instances/{w.wid}", headers=H, json={**base, "restart_config": {"url": "http://h:8399", "token_file": str(tok)}})
    assert ok.status_code == 200, ok.text
    async with farm["router"].db.conn() as c:
        dump = await c.fetchval("SELECT string_agg(row_to_json(t)::text, '') FROM (SELECT * FROM instances) t")
        audit = await c.fetchval("SELECT string_agg(detail::text, '') FROM audit_log")
    assert "s3cret" not in dump and "t" * 32 not in dump and "t" * 32 not in (audit or "")
    from comfyfarm.rollout import backends as be
    assert be.make("http-agent", {"url": "http://h:8399", "token_file": str(tok)}, keys_dir=str(tmp_path)).token() == "t" * 32
    with pytest.raises(be.BackendError, match="empty"):
        be.make("http-agent", {"url": "http://h", "token_env": "COMFYFARM_AGENT_TOKEN_TEST_NONE"}).token()


async def test_backend_config_cannot_reach_beyond_its_references(farm, tmp_path):
    """QA-7: an ssh user/host must never become an ssh option (command execution on the router host), and secret
    references must stay in the keys directory / COMFYFARM_AGENT_TOKEN… variables — the token is sent to `url`."""
    from comfyfarm.rollout import backends as be
    api, w = farm["api"], farm["hosts"]["w1"]
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "gpu1.key").write_text("k")
    (keys / "agent.token").write_text("t" * 32)
    (tmp_path / "elsewhere.txt").write_text("not a key")
    (keys / "sneaky").symlink_to(tmp_path / "elsewhere.txt")
    farm["router"].s.keys_dir = str(keys)
    cfg = (await instances(api))["w1"]["instance"]

    async def put(backend, config):
        return await api.put(f"/api/admin/instances/{w.wid}", headers=H, json={**cfg, "restart_backend": backend,
                                                                               "restart_config": config})
    ssh_ok = {"host": "gpu-1.farm.lan", "user": "comfy", "key": "gpu1.key", "port": 22}
    for good in (ssh_ok, {**ssh_ok, "host": "10.0.0.5", "key": str(keys / "gpu1.key")}, {**ssh_ok, "host": "fe80::1"}):
        assert (await put("ssh", good)).status_code == 200, good                       # positive controls
    for bad in ({**ssh_ok, "user": "-oProxyCommand=touch /tmp/pwned"}, {**ssh_ok, "host": "-oProxyCommand=id"},
                {**ssh_ok, "user": "a b"}, {**ssh_ok, "host": "h;id"}, {**ssh_ok, "port": "22 -oX=y"},
                {**ssh_ok, "key": "/etc/ssh/ssh_host_ed25519_key"}, {**ssh_ok, "key": "../elsewhere.txt"},
                {**ssh_ok, "key": "sneaky"}, {**ssh_ok, "known_hosts": "/etc/passwd"}):
        res = await put("ssh", bad)
        assert res.status_code == 400, bad
        with pytest.raises(be.BackendError):
            be.make("ssh", bad, keys_dir=str(keys))
    argv = be.make("ssh", ssh_ok, keys_dir=str(keys)).argv("restart", "w1")
    assert argv[-5:] == ["-l", "comfy", "--", "gpu-1.farm.lan", "restart w1"] and argv[2] == str((keys / "gpu1.key").resolve())
    agent_ok = {"url": "http://gpu-1:8399", "token_file": "agent.token"}
    assert (await put("http-agent", agent_ok)).status_code == 200
    assert (await put("http-agent", {"url": "http://gpu-1:8399", "token_env": "COMFYFARM_AGENT_TOKEN_GPU1"})).status_code == 200
    for bad in ({**agent_ok, "token_file": str(tmp_path / "elsewhere.txt")}, {**agent_ok, "token_file": "/proc/self/environ"},
                {**agent_ok, "token_file": "sneaky"}, {"url": "http://gpu-1:8399", "token_env": "COMFYFARM_DSN"},
                {"url": "http://gpu-1:8399", "token_env": "AWS_SECRET_ACCESS_KEY"}, {**agent_ok, "url": "file:///etc/passwd"}):
        res = await put("http-agent", bad)
        assert res.status_code == 400, bad
        with pytest.raises(be.BackendError):
            be.make("http-agent", bad, keys_dir=str(keys))
    # the engine builds backends with the configured keys directory too (a ledger written before this check)
    from comfyfarm.rollout.engine import Engine
    eng = Engine(farm["router"])                                  # the fixture replaced the running engine's factory
    try:
        with pytest.raises(be.BackendError, match="keys directory"):
            eng.make_backend("http-agent", {"url": "http://x", "token_file": str(tmp_path / "elsewhere.txt")})
        assert eng.make_backend("http-agent", agent_ok).token() == "t" * 32
    finally:
        await eng.stop()
    with pytest.raises(be.BackendError):
        be.make("systemd-local", {"unit": "--host=evil@{instance}"})


async def step_status(r, rid, wid):
    async with r.db.conn() as c:
        return await c.fetchval("SELECT status FROM rollout_steps WHERE rollout_id = $1 AND worker_id = $2", rid, wid)


async def wait_for(cond, timeout=30.0):
    t = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t < timeout:
        if await cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


async def test_engine_error_while_reverting_waits_for_a_person_then_reverts(farm):
    """QA-3: an engine error (database busy, connection lost) must not let the rollout pick itself up again: a step
    caught while reverting would be driven forward and the rollout would carry on to the fleet (D-38)."""
    import contextlib
    from comfyfarm.db import Busy
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    w1, w2 = hosts["w1"], hosts["w2"]
    w1.modes = ["broken", "noop"]                    # verify fails → revert; the revert's restart hangs (not seen down)
    eng, real = r.rollout, r.rollout.db
    crashed = []

    class FlakyDb:                                   # the driver's next database call fails once the step reverts
        @contextlib.asynccontextmanager
        async def conn(self, *a, **kw):
            if not crashed and asyncio.current_task().get_coro().__qualname__ == "Engine.run" and \
                    await step_status(r, crashed_rid[0], w1.wid) == "reverting":
                crashed.append(1)
                raise Busy("database pool exhausted")
            async with real.conn(*a, **kw) as c:
                yield c
    crashed_rid = [None]
    eng.db = FlakyDb()
    rid = crashed_rid[0] = await start(api, farm["p2"], [w1.wid, w2.wid])
    await wait_for(lambda: _status_is(api, rid, "interrupted"))
    await asyncio.sleep(2.5)                                           # nobody drives it on its own
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert ro["status"] == "interrupted" and "engine error" in ro["error"], ro
    assert [s["status"] for s in ro["steps"]] == ["reverting", "pending"] and w2.restarts == 0
    eng.db = real
    assert (await api.post(f"/api/admin/rollouts/{rid}/resume", headers=H)).status_code == 200
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "failed" and [s["status"] for s in ro["steps"]] == ["reverted", "skipped"], ro["steps"]
    assert w2.restarts == 0
    inst = await instances(api)
    assert inst["w1"]["profile"] == "p1" and inst["w1"]["badge"] == "match"       # back, never forward


async def _status_is(api, rid, status):
    return (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()["status"] == status


async def test_recorded_failure_survives_an_engine_error(farm):
    """QA-3: the canary stop must not live only in the driver's memory — after an engine error and a resume, a
    failed step still keeps the remaining workers from being touched."""
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    eng, real = r.rollout, r.rollout.finish
    calls = []

    async def finish(rid, actor):
        calls.append(rid)
        if len(calls) == 1:
            raise ConnectionResetError("connection to the database lost")
        return await real(rid, actor)
    eng.finish = finish
    p3 = (await api.post("/api/admin/profiles", json={"name": "p3", "core": "c2", "venv": "v-missing"}, headers=H)).json()["id"]
    rid = await start(api, p3, [hosts["w1"].wid, hosts["w2"].wid])
    await wait_for(lambda: _status_is(api, rid, "interrupted"))
    await asyncio.sleep(2.5)
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert ro["status"] == "interrupted" and [s["status"] for s in ro["steps"]] == ["reverted", "pending"], ro
    assert hosts["w2"].restarts == 0
    assert (await api.post(f"/api/admin/rollouts/{rid}/resume", headers=H)).status_code == 200
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "failed" and [s["status"] for s in ro["steps"]] == ["reverted", "skipped"], ro["steps"]
    assert hosts["w2"].restarts == 0


async def test_abort_reaches_a_step_queued_on_the_start_lock(farm):
    """QA-9: a step waiting for the startup lock is still before its restart — Abort must stop it there."""
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    await r.rollout.start_lock.acquire()                    # another startup is in progress
    try:
        rid = await start(api, farm["p2"], [hosts["w1"].wid])
        await asyncio.sleep(4.0)                            # drained, now queued on the lock
        assert (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()["steps"][0]["status"] == "draining"
        assert (await api.post(f"/api/admin/rollouts/{rid}/abort", headers=H)).json()["status"] == "aborting"
        await asyncio.sleep(0.5)
    finally:
        r.rollout.start_lock.release()
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "aborted" and ro["steps"][0]["status"] == "skipped", ro["steps"]
    assert hosts["w1"].restarts == 0
    assert not any(w["maintenance"] for w in (await api.get("/api/admin/workers", headers=H)).json())


async def test_recheck_under_the_start_lock_never_holds_it(farm, monkeypatch):
    """QA-9: with drain_timeout 0 (wait forever) a worker whose queue fills right before the restart must not keep the
    startup lock: the step goes back to draining and the lock is free for the others."""
    from comfyfarm.rollout import engine as engine_mod
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    monkeypatch.setattr(engine_mod, "QUICK_DRAIN", 1.0, raising=False)
    seen = []

    async def queue_empty(base_url):
        seen.append(base_url)
        return len(seen) <= 2                               # the full drain passes, then a job is always there
    r.rollout.worker_queue_empty = queue_empty
    rid = await start(api, farm["p2"], [hosts["w1"].wid], options={"drain_timeout": 0})
    await asyncio.sleep(6.0)
    assert not r.rollout.start_lock.locked()
    ro = (await api.get(f"/api/admin/rollouts/{rid}", headers=H)).json()
    assert ro["steps"][0]["status"] == "draining" and hosts["w1"].restarts == 0
    assert (await api.post(f"/api/admin/rollouts/{rid}/abort", headers=H)).status_code == 200
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "aborted" and hosts["w1"].restarts == 0


async def test_revert_target_is_in_use_during_a_rollout(farm):
    """QA-4: the profile a step would revert to must not be editable while the rollout is active (D-36)."""
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    p_old = (await api.post("/api/admin/profiles", json={"name": "old", "core": "c1", "venv": "v1"}, headers=H)).json()["id"]
    assert (await api.patch(f"/api/admin/profiles/{p_old}", json={"venv": "v2"}, headers=H)).status_code == 200   # unused
    async with r.db.conn() as c:                     # w1 was on `old` when the (now interrupted) rollout started
        rid = await c.fetchval("INSERT INTO rollouts(target_profile_id, created_by, status) VALUES ($1, 'x', 'interrupted') "
                               "RETURNING id", farm["p2"])
        await c.execute("INSERT INTO rollout_steps(rollout_id, worker_id, position, from_profile_id, to_profile_id, status) "
                        "VALUES ($1, $2, 0, $3, $4, 'starting')", rid, hosts["w1"].wid, p_old, farm["p2"])
    res = await api.patch(f"/api/admin/profiles/{p_old}", json={"venv": "v1"}, headers=H)
    assert res.status_code == 409, res.text
    assert (await api.patch(f"/api/admin/profiles/{p_old}", json={"note": "revert target"}, headers=H)).status_code == 200


async def test_worker_changes_mid_step_are_refused_and_everything_is_audited(farm):
    """QA-10: a rollout step owns its worker's maintenance flag and address (409); worker changes, instance
    configurations (incl. restart_config: references only) and the abort of an interrupted rollout are audited."""
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    w1, w2 = hosts["w1"], hosts["w2"]
    async with r.db.conn() as c:
        rid = await c.fetchval("INSERT INTO rollouts(target_profile_id, created_by, status) VALUES ($1, 'x', 'interrupted') "
                               "RETURNING id", farm["p2"])
        await c.execute("UPDATE workers SET maintenance = true")
        await c.execute("INSERT INTO rollout_steps(rollout_id, worker_id, position, to_profile_id, status, was_maintenance) VALUES "
                        "($1, $2, 0, $4, 'starting', false), ($1, $3, 1, $4, 'draining', false)", rid, w1.wid, w2.wid, farm["p2"])
    for change in ({"maintenance": False}, {"base_url": "http://127.0.0.1:1"}):
        res = await api.patch(f"/api/admin/workers/{w1.wid}", json=change, headers=H)
        assert res.status_code == 409, (change, res.text)
    assert (await api.patch(f"/api/admin/workers/{w1.wid}", json={"note": "flaky fan"}, headers=H)).status_code == 200
    assert (await api.patch(f"/api/admin/workers/{w1.wid}", json={"enabled": False}, headers=H)).status_code == 200
    assert (await api.post(f"/api/admin/rollouts/{rid}/abort", headers=H)).json()["status"] == "aborted"
    assert (await api.patch(f"/api/admin/workers/{w1.wid}", json={"maintenance": False, "enabled": True}, headers=H)).status_code == 200
    cfg = (await instances(api))["w2"]["instance"]
    assert (await api.put(f"/api/admin/instances/{w2.wid}", headers=H,
                          json={**cfg, "restart_config": {"timeout": 77}})).status_code == 200
    log = (await api.get("/api/admin/audit", headers=H)).json()
    changed = [a for a in log if a["action"] == "worker changed" and a["target"] == "w1"]
    assert {"note": "flaky fan"} in [a["detail"] for a in changed] and {"enabled": False} in [a["detail"] for a in changed]
    assert {"maintenance": False, "enabled": True} in [a["detail"] for a in changed]
    assert any(a["action"] == "maintenance off" and a["target"] == "w2" for a in log)        # the abort put w2 back
    conf = next(a for a in log if a["action"] == "instance configured" and a["target"] == "w2")
    assert conf["detail"]["config"]["restart_config"] == {"timeout": 77}


async def test_a_worker_that_is_not_running_can_be_started_a_running_one_cannot(farm):
    """Field: a worker added before any profile existed was assigned one later — and nothing on the screen started it.
    Start is for a worker that is not running; a running one changes through a rollout, which drains it first."""
    api, r, hosts = farm["api"], farm["router"], farm["hosts"]
    w1 = hosts["w1"].wid
    res = await api.post(f"/api/admin/instances/{w1}/start", headers=H)
    assert res.status_code == 409 and "rollout" in res.text
    await hosts["w1"].stop()
    async with r.db.conn() as c:
        await c.execute("UPDATE workers SET state = 'offline' WHERE id = $1", w1)
    res = await api.post(f"/api/admin/instances/{w1}/start", headers=H)
    assert res.status_code == 200 and res.json()["started"], res.text
    audit = (await api.get("/api/admin/audit?limit=5", headers=H)).json()
    rows = audit if isinstance(audit, list) else audit.get("rows", audit.get("entries", []))
    assert any(a.get("action") == "instance started" for a in rows), rows
    w3 = (await api.post("/api/admin/workers", json={"name": "w3", "base_url": "http://127.0.0.1:9"}, headers=H)).json()["id"]
    assert (await api.post(f"/api/admin/instances/{w3}/start", headers=H)).status_code == 400   # nothing assigned


async def test_a_missing_command_is_a_failed_delivery_not_a_crash():
    """Field (local check): Start on a host without systemctl answered HTTP 500 instead of saying why."""
    from comfyfarm.rollout import backends as be
    rc, out = await be._run(["/nonexistent/systemctl", "restart", "x"])
    assert rc == 127 and "cannot run /nonexistent/systemctl" in out



async def test_restart_option_restarts_a_worker_that_runs_the_target(farm):
    """Field (dev.10): after Set up again (new tools, a new environment file) a rollout of the same profile said
    "already running the target" and left the old process running. restart: true restarts it anyway — drained and
    verified like any step."""
    api, h = farm["api"], farm["hosts"]["w1"]
    before = h.status["NRestarts"]
    ro = await wait_rollout(api, await start(api, farm["p1"], [h.wid]))
    assert ro["status"] == "succeeded" and h.status["NRestarts"] == before                  # left as it was
    ro = await wait_rollout(api, await start(api, farm["p1"], [h.wid], options={"restart": True}))
    assert ro["status"] == "succeeded" and h.status["NRestarts"] == before + 1, ro["steps"][0]
    bad = await api.post("/api/admin/rollouts", headers=H, json={"profile_id": farm["p1"], "worker_ids": [h.wid],
                                                                 "options": {"restart": "yes"}})
    assert bad.status_code == 400


def copying_backend(hosts, script, seen):
    """FakeHostBackend whose host keeps copies (docs/design/16): `script` answers the state asks one after the other;
    `seen` records each ask with the step's state and the worker's maintenance flag at that moment."""
    from comfyfarm.testing.fake_host import FakeHostBackend

    class Copying(FakeHostBackend):
        async def fetch(self, artifacts, state=False):
            seen.append(("state" if state else "request", sorted(artifacts)))
            if not state:
                return {"requested": True}
            return script.pop(0) if len(script) > 1 else script[0]
    return Copying(hosts)


async def test_copies_come_before_the_drain(farm):
    """docs/design/16: the target's artifacts are copied while the worker still works; it is drained and restarted
    once they are there. The step says how far the copy got."""
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    seen, steps = [], []
    script = [{"core/c2": {"state": "present"}, "venv/v2": {"state": "missing"}},
              {"core/c2": {"state": "present"}, "venv/v2": {"state": "running", "bytes": 2**30, "total": 2 * 2**30}},
              {"core/c2": {"state": "present"}, "venv/v2": {"state": "present"}}]
    r.rollout.make_backend = lambda _n, _c: copying_backend(hosts, script, seen)
    r.rollout.fetch_poll = 0.1
    orig = r.rollout.set_step

    async def watch(sid, status, detail=None, error=None):
        if status == "fetching":
            async with r.db.conn() as c:
                steps.append((detail, await c.fetchval("SELECT maintenance FROM workers WHERE id = $1", hosts["w1"].wid)))
        await orig(sid, status, detail, error)
    r.rollout.set_step = watch
    rid = await start(api, farm["p2"], [hosts["w1"].wid])
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "succeeded", ro
    assert seen[0] == ("state", ["core/c2", "venv/v2"]) and seen[1] == ("request", ["core/c2", "venv/v2"])
    assert steps and all(m is False for _, m in steps)                     # never out of service while copying
    assert "copying 1 of 2 artifact(s) · 1.0/2.0 GB" in [d.split(" —")[0] for d, _ in steps]
    assert hosts["w1"].restarts == 1


async def test_a_failed_copy_fails_the_step_and_changes_nothing(farm):
    api, hosts, r = farm["api"], farm["hosts"], farm["router"]
    seen = []
    script = [{"venv/v2": {"state": "missing"}, "core/c2": {"state": "missing"}},
              {"venv/v2": {"state": "failed", "error": "fetch: the list of files is not signed by this farm's builder"},
               "core/c2": {"state": "present"}}]
    r.rollout.make_backend = lambda _n, _c: copying_backend(hosts, script, seen)
    r.rollout.fetch_poll = 0.1
    rid = await start(api, farm["p2"], [hosts["w1"].wid])
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "failed" and ro["steps"][0]["status"] == "failed"
    assert "copies of the artifacts: copying venv/v2 failed: fetch: the list of files is not signed" in ro["steps"][0]["error"]
    assert hosts["w1"].restarts == 0
    w = next(x for x in (await api.get("/api/admin/workers", headers=H)).json() if x["id"] == hosts["w1"].wid)
    assert w["maintenance"] is False
    assert (await instances(api))["w1"]["profile"] == "p1"                 # still on its profile


async def test_the_audit_log_comes_a_page_at_a_time(farm):
    """A long audit log is paged; an older page stays put while new entries come in (upto)."""
    api, r = farm["api"], farm["router"]
    async with r.db.conn() as c:
        await c.execute("DELETE FROM audit_log")
        for i in range(45):
            await c.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES ('t', 'step', $1, '{}')", str(i))
    first = (await api.get("/api/admin/audit/page?limit=20", headers=H)).json()
    assert first["total"] == 45 and [e["target"] for e in first["items"]][:2] == ["44", "43"] and len(first["items"]) == 20
    upto = first["upto"]
    async with r.db.conn() as c:                                  # new entries while one reads page 2
        for i in range(45, 50):
            await c.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES ('t', 'step', $1, '{}')", str(i))
    p2 = (await api.get(f"/api/admin/audit/page?limit=20&offset=20&upto={upto}", headers=H)).json()
    assert p2["total"] == 45 and [e["target"] for e in p2["items"]][0] == "24"     # where it was, not 5 further
    p3 = (await api.get(f"/api/admin/audit/page?limit=20&offset=40&upto={upto}", headers=H)).json()
    assert [e["target"] for e in p3["items"]] == ["4", "3", "2", "1", "0"]
    live = (await api.get("/api/admin/audit/page?limit=20", headers=H)).json()     # the first page shows them
    assert live["total"] == 50 and live["items"][0]["target"] == "49"
    assert (await api.get("/api/admin/audit/page", headers={"X-ComfyFarm-User": "mallory"})).status_code == 403


async def test_a_rollout_after_an_assign_goes_back_to_what_ran(farm):
    """Field, dev.23: the new profile was *assigned* first (no restart), then rolled out. The step took the assignment
    as where it came from, so the failed rollout "reverted" to the new, broken profile and the badge said match. The
    way back is what the worker runs — the profile whose render its argv matches."""
    api, hosts = farm["api"], farm["hosts"]
    w1 = hosts["w1"]
    res = await api.post(f"/api/admin/instances/{w1.wid}/assign", json={"profile_id": farm["p2"]}, headers=H)
    assert any("keeps running its current profile" in x for x in res.json()["warnings"])     # said at assign time
    assert (await instances(api))["w1"]["badge"] == "differs"                  # assigned p2, still running p1
    w1.modes = ["node_import_failed"]
    rid = await start(api, farm["p2"], [w1.wid])
    ro = await wait_rollout(api, rid)
    step = ro["steps"][0]
    assert ro["status"] == "failed" and step["from"] == "p1", step
    assert step["status"] == "reverted" and "back on p1" in step["detail"]
    inst = await instances(api)
    assert inst["w1"]["profile"] == "p1" and inst["w1"]["badge"] == "match"    # really back on the old one


async def test_a_failed_rollout_of_what_already_ran_says_there_is_no_way_back(farm):
    """A worker restarted into the profile it already ran (the restart option): a failure has nothing earlier to
    return to. Not "reverted" — it stays, out of service, and the step says why."""
    api, hosts = farm["api"], farm["hosts"]
    w1 = hosts["w1"]
    w1.modes = ["node_import_failed"]
    rid = await start(api, farm["p1"], [w1.wid], options={"restart": True})
    ro = await wait_rollout(api, rid)
    step = ro["steps"][0]
    assert step["from"] == step["to"] == "p1"
    assert step["status"] == "revert_failed" and "no earlier profile to go back to: stays on p1" in step["detail"], step
    workers = {w["name"]: w for w in (await api.get("/api/admin/workers", headers=H)).json()}
    assert workers["w1"]["maintenance"]


async def test_a_worker_in_maintenance_before_the_rollout_stays_in_it_and_says_so(farm):
    """Field (dev.24): a failed rollout left the worker in maintenance; the next ones succeeded and left it there —
    restored as it was — so it took no jobs for hours while its rollouts read 'done'. And its node definitions were
    never read: only workers in service were asked. D-46: the step says it is still in maintenance; the read after the
    rollout asks the worker it verified, in maintenance or not."""
    api, r = farm["api"], farm["router"]
    w1 = farm["hosts"]["w1"]
    async with r.db.conn() as c:
        await c.execute("UPDATE workers SET maintenance = true WHERE id = $1", w1.wid)
        await c.execute("UPDATE object_info_snapshots SET stale = true WHERE group_id = $1", farm["gid"])
    rid = await start(api, farm["p2"], [w1.wid])
    ro = await wait_rollout(api, rid)
    assert ro["status"] == "succeeded", ro
    (step,) = ro["steps"]
    assert step["status"] == "done" and step["detail"] == ("running p2; still in maintenance as before the rollout: it "
                                                           "takes no jobs until maintenance is turned off")
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT maintenance FROM workers WHERE id = $1", w1.wid)
        snap = await c.fetchrow("SELECT source, stale FROM object_info_snapshots WHERE group_id = $1", farm["gid"])
    assert snap is not None and snap["source"] == "w1" and not snap["stale"]
    # a worker in service before the rollout: back in service, and the step says only what it runs
    w2 = farm["hosts"]["w2"]
    ro = await wait_rollout(api, await start(api, farm["p2"], [w2.wid]))
    assert [s["detail"] for s in ro["steps"]] == ["running p2"]
