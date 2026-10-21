"""Builds on the control plane, node packs from the dashboard and the UI worker container (docs/design/15)."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest

from comfyfarm import agent, builder, publish, uiworker
from comfyfarm.app import create_app
from conftest import fast_settings, free_port

H = {"X-ComfyFarm-User": "admin"}
C1, C2 = "a" * 40, "b" * 40


# ---- the builder ------------------------------------------------------------------------------------------------
@pytest.fixture
def box(tmp_path):
    for d in ("requests", "status", "art", "share/recipes"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "share/recipes/rocm-x.json").write_text("{}")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        kw["stderr"].write(b"building...\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"template": "rocm-x-1"}).encode(), b"")
    b = builder.Builder(str(tmp_path / "requests"), str(tmp_path / "status"), str(tmp_path / "art"), str(tmp_path / "share"),
                        "/usr/bin/uv", ["https://github.com/"], "x86_64-manylinux_2_35", runner=runner)
    return b, tmp_path, calls


def ask(tmp_path, doc, rid=None):
    rid = rid or f"{int(time.time() * 1000)}-{os.urandom(4).hex()}"
    (tmp_path / "requests" / f"{rid}.json").write_text(json.dumps(doc))
    return rid


def test_the_builder_builds_installed_recipes_with_packs_from_allowed_sources_only(box, capsys):
    b, tmp, calls = box
    good = {"name": "VHS", "repo": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite", "commit": C1}
    rid = ask(tmp, {"kind": "build", "recipe": "rocm-x", "roles": ["ui"], "nodes": [good]})
    assert b.pending("build") == [rid]
    st = b.handle(rid)
    assert st["state"] == "done" and st["template"] == "rocm-x-1" and st["nodes"] == 1, st
    said = capsys.readouterr().out.splitlines()          # its own log: what it took and how it ended (QA: it said nothing)
    assert said == [f"comfyfarm builder: {rid}: build rocm-x (ui, 1 node pack(s))",
                    f"comfyfarm builder: {rid}: done after 0 s — template rocm-x-1"]
    argv = calls[-1]
    assert argv[argv.index("--roles") + 1] == "ui" and argv[argv.index("--platform") + 1] == "x86_64-manylinux_2_35"
    assert json.loads(open(argv[argv.index("--nodes-json") + 1]).read()) == [good]
    assert (tmp / "status" / f"{rid}.log").read_bytes() == b"building...\n" and b.pending("build") == []
    for bad, why in [({"recipe": "not-installed"}, "no recipe"),
                     ({"recipe": "rocm-x", "roles": ["gpu", "root"]}, "roles"),
                     ({"recipe": "rocm-x", "nodes": [{**good, "repo": "https://evil.example/x"}]}, "not under the allowed"),
                     ({"recipe": "rocm-x", "nodes": [{**good, "repo": "https://github.com/../x"}]}, "not under the allowed"),
                     ({"recipe": "rocm-x", "nodes": [{**good, "post_install": "curl | sh"}]}, "only commit"),
                     ({"recipe": "rocm-x", "nodes": "all"}, "a list")]:
        st = b.handle(ask(tmp, {"kind": "build", **bad}))
        assert st["state"] == "failed" and why in st["error"], (bad, st)
    b.node_sources = []                                    # a strict farm: no packs from the dashboard at all
    st = b.handle(ask(tmp, {"kind": "build", "recipe": "rocm-x", "nodes": [good]}))
    assert st["state"] == "failed" and "off for this farm" in st["error"]
    assert len(calls) == 1                                 # nothing refused ever reached the build


def test_the_builder_resolves_a_branch_or_tag_to_a_commit(box, monkeypatch):
    b, tmp, _ = box
    out = f"{C1}\tHEAD\n{C2}\trefs/heads/main\n{'c' * 40}\trefs/tags/v1\n{'d' * 40}\trefs/tags/v1^{{}}\n"
    seen = []
    monkeypatch.setattr(publish, "git_net", lambda argv, env=None, tries=None: (seen.append(argv),
                        subprocess.CompletedProcess(argv, 0, out, ""))[1])
    repo = "https://github.com/x/y"
    assert b.handle(ask(tmp, {"kind": "resolve", "repo": repo, "ref": ""}))["commit"] == C1
    assert b.handle(ask(tmp, {"kind": "resolve", "repo": repo, "ref": "main"}))["commit"] == C2
    assert b.handle(ask(tmp, {"kind": "resolve", "repo": repo, "ref": "v1"}))["commit"] == "d" * 40     # the peeled tag
    assert seen[0][:4] == ["git", "ls-remote", "--end-of-options", repo]
    for ref in ("--upload-pack=sh", "nope", "a b"):
        assert b.handle(ask(tmp, {"kind": "resolve", "repo": repo, "ref": ref}))["state"] == "failed"
    assert b.handle(ask(tmp, {"kind": "resolve", "repo": "https://gitlab.com/x/y", "ref": "main"}))["state"] == "failed"


def test_a_build_running_when_the_builder_stopped_is_failed_not_left_running(box):
    b, tmp, _ = box
    rid = ask(tmp, {"kind": "build", "recipe": "rocm-x"})
    builder.write_json(str(tmp / "status" / f"{rid}.json"), {"id": rid, "state": "running"})
    (tmp / "requests" / "1700000000000-deadbeef.json").write_text("not json")
    b.recover()
    assert json.loads((tmp / "status" / f"{rid}.json").read_text())["state"] == "failed"
    assert b.pending("build") == []
    assert json.loads((tmp / "status" / "1700000000000-deadbeef.json").read_text())["error"] == "unreadable request"


# ---- the router's side ----------------------------------------------------------------------------------------
def publish_fake(art, kind, name):
    d = art / kind / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"contract": "comfy-upgrade/1", "kind": kind, "name": name}))
    (d / "READY").write_text("")


@pytest.fixture
async def farm(dsn, tmp_path):
    builds, art, keys = tmp_path / "builds", tmp_path / "art", tmp_path / "keys"
    for d in (builds / "requests", builds / "status", art, keys):
        d.mkdir(parents=True)
    app = create_app(fast_settings(dsn, builds_dir=str(builds), artifacts_dir=str(art), keys_dir=str(keys)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield app.state.router, c, builds, art


def builder_up(builds, sources=("https://github.com/",), recipes=("rocm-x",)):
    builder.write_json(str(builds / "status" / "policy.json"), {"recipes": list(recipes), "node_sources": list(sources)})


async def test_builds_and_node_packs_through_the_router(farm):
    r, c, builds, art = farm
    assert (await c.post("/api/admin/builds", json={"recipe": "rocm-x"}, headers=H)).status_code == 503   # no builder yet
    builder_up(builds)
    st = (await c.get("/api/admin/builder", headers=H)).json()
    assert st["up"] and st["recipes"] == ["rocm-x"] and st["node_sources"] == ["https://github.com/"]
    pack = {"name": "VHS", "repo": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite", "commit": C1, "ref": "main"}
    for bad in ({**pack, "commit": "main"}, {**pack, "repo": "http://x/y"}, {**pack, "name": "../x"},
                {**pack, "constraints": ["--index-url=x"]}, {**pack, "requirements_file": "../../etc/passwd.txt"},
                {**pack, "name": "comfyfarm_worker"}):
        assert (await c.post("/api/admin/node-packs", json=bad, headers=H)).status_code == 400, bad
    pid = (await c.post("/api/admin/node-packs", json={**pack, "constraints": ["kornia<0.8"]}, headers=H)).json()["id"]
    assert (await c.post("/api/admin/node-packs", json=pack, headers=H)).status_code == 409
    gpu_only = {"name": "RTX", "repo": "https://github.com/Comfy-Org/Nvidia_RTX_Nodes_ComfyUI", "commit": C2, "ui": False}
    await c.post("/api/admin/node-packs", json=gpu_only, headers=H)
    assert (await c.patch(f"/api/admin/node-packs/{pid}", json={"commit": C2}, headers=H)).status_code == 200
    assert (await c.patch(f"/api/admin/node-packs/{pid}", json={"commit": "x"}, headers=H)).status_code == 400
    listed = (await c.get("/api/admin/node-packs", headers=H)).json()
    assert [(p["name"], p["commit"], p["ui"]) for p in listed] == [("RTX", C2, False), ("VHS", C2, True)]
    # a build request carries the packs; the builder's status comes back with the log's tail
    rid = (await c.post("/api/admin/builds", json={"recipe": "rocm-x"}, headers=H)).json()["id"]
    req = json.loads((builds / "requests" / f"{rid}.json").read_text())
    assert req["kind"] == "build" and [n["name"] for n in req["nodes"]] == ["RTX", "VHS"]
    assert req["nodes"][1]["constraints"] == ["kornia<0.8"] and req["nodes"][0]["ui"] is False
    assert (await c.get(f"/api/admin/builds/{rid}", headers=H)).json()["state"] == "queued"
    builder.write_json(str(builds / "status" / f"{rid}.json"), {"id": rid, "kind": "build", "state": "done", "template": "rocm-x-1"})
    (builds / "status" / f"{rid}.log").write_text("x" * 10000 + "the end\n")
    got = (await c.get(f"/api/admin/builds/{rid}", headers=H)).json()
    assert got["template"] == "rocm-x-1" and got["log"].endswith("the end\n") and len(got["log"]) <= 6000
    assert [b["id"] for b in (await c.get("/api/admin/builds", headers=H)).json()] == [rid]
    assert (await c.post("/api/admin/builds", json={"recipe": "other"}, headers=H)).status_code == 400
    # resolve goes to the builder, only for allowed sources
    assert (await c.post("/api/admin/node-packs/resolve", json={"repo": "https://gitlab.com/a/b"}, headers=H)).status_code == 400
    rid = (await c.post("/api/admin/node-packs/resolve", json={"repo": "https://github.com/a/b", "ref": "v1"}, headers=H)).json()["id"]
    assert json.loads((builds / "requests" / f"{rid}.json").read_text())["kind"] == "resolve"
    # a builder that takes no packs: the request is refused before it is written
    builder_up(builds, sources=())
    assert (await c.post("/api/admin/builds", json={"recipe": "rocm-x"}, headers=H)).status_code == 400
    assert (await c.post("/api/admin/builds", json={"recipe": "rocm-x", "packs": False}, headers=H)).status_code == 200
    assert (await c.delete(f"/api/admin/node-packs/{pid}", headers=H)).status_code == 200
    assert (await c.get("/api/admin/node-packs", headers={"X-ComfyFarm-User": "mallory"})).status_code == 403


async def test_templates_are_read_from_the_store(farm):
    r, c, builds, art = farm
    (art / "profiles").mkdir()
    for kind, name in (("core", "comfyui-0.41.0"), ("venv", "cpu-1"), ("nodes", "farm-ui-1")):
        publish_fake(art, kind, name)
    ui = {"core": "comfyui-0.41.0", "venv": "cpu-1", "nodes": ["farm-ui-1"], "flags": ["--cpu"], "env": {}}
    (art / "profiles" / "rocm-x-1.json").write_text(json.dumps({"template": "rocm-x-1", "recipe": "rocm-x", "ui": ui}))
    (art / "profiles" / "rocm-x-2.json").write_text(json.dumps({"template": "rocm-x-2", "recipe": "rocm-x", "ui": ui,
                                                                "gpu": {**ui, "venv": "rocm72-1"}}))
    t = {x["template"]: x for x in (await c.get("/api/admin/templates", headers=H)).json()}
    assert t["rocm-x-1"]["roles"] == ["ui"] and t["rocm-x-1"]["missing"] == []
    assert t["rocm-x-2"]["roles"] == ["gpu", "ui"] and t["rocm-x-2"]["missing"] == ["venv/rocm72-1"]


async def test_a_host_name_in_hosts_is_looked_up_for_the_launcher(farm):
    """The compose UI worker's instance names its host `ui`: the launcher's request comes from that address."""
    r, c, builds, art = farm
    wid = (await c.post("/api/admin/workers", json={"name": "named", "base_url": "http://localhost:18999", "kind": "ui"},
                        headers=H)).json()["id"]
    paths = {k: f"/srv/x/{k}" for k in ("art", "local", "models", "input", "output", "user", "temp")}
    assert (await c.put(f"/api/admin/instances/{wid}", json={"instance": "named-1", "role": "ui", "hosts": ["localhost"],
                                                             "port": 18999, "paths": paths}, headers=H)).status_code == 200
    from comfyfarm.api.builds import resolve_hosts
    assert "127.0.0.1" in await resolve_hosts(["localhost", "10.0.0.1"]) and "10.0.0.1" not in await resolve_hosts(["10.0.0.1"])


# ---- the UI worker container: first start ---------------------------------------------------------------------
class FakeAgent:
    def __init__(self, port, token):
        self.calls = []
        self.port = port
        threading.Thread(target=agent.serve, args=(port, token, "127.0.0.1", self.do), daemon=True).start()

    def do(self, verb, instance):
        self.calls.append((verb, instance))
        if verb == "status":
            return 0, json.dumps({"ActiveState": "active", "ExecMainStatus": "0", "NRestarts": "0"})
        return 0, "restart requested"


async def test_the_ui_worker_container_is_registered_built_for_and_started(dsn, tmp_path, fakes):
    """A new farm: no UI worker, nothing in the store. The router registers the container's UI worker, asks the
    builder for the UI half, and once the template is there makes the profile, assigns it and starts the worker."""
    builds, art, keys = tmp_path / "builds", tmp_path / "art", tmp_path / "keys"
    for d in (builds / "requests", builds / "status", art, keys):
        d.mkdir(parents=True)
    (keys / "ui_agent_token").write_text("t" * 32)
    ui = await fakes()
    port = free_port()
    fake_agent = FakeAgent(port, "t" * 32)
    builder_up(builds)
    app = create_app(fast_settings(dsn, builds_dir=str(builds), artifacts_dir=str(art), keys_dir=str(keys),
                                   ui_service=ui.url, ui_agent=f"http://127.0.0.1:{port}", ui_shared=str(tmp_path / "shared"),
                                   ui_home=str(tmp_path / "uihome"), first_recipe="rocm-x"))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            for _ in range(50):                              # the first step runs at start
                reqs = list((builds / "requests").glob("*.json"))
                if reqs:
                    break
                await asyncio.sleep(0.1)
            workers = (await c.get("/api/admin/workers", headers=H)).json()
            assert [(w["name"], w["kind"]) for w in workers] == [("ui", "ui")]
            req = json.loads(reqs[0].read_text())
            assert req["roles"] == ["ui"] and req["recipe"] == "rocm-x" and req["nodes"] == []
            assert r.first_start["state"] == "building"
            await r.first_start_step()                      # still queued: no second request
            assert len(list((builds / "requests").glob("*.json"))) == 1
            # the builder finished: the template's UI half is in the store
            (art / "profiles").mkdir()
            for kind, name in (("core", "comfyui-0.41.0"), ("venv", "cpu-1"), ("nodes", "farm-ui-1")):
                publish_fake(art, kind, name)
            (art / "profiles" / "rocm-x-1.json").write_text(json.dumps({"template": "rocm-x-1", "recipe": "rocm-x", "ui": {
                "core": "comfyui-0.41.0", "venv": "cpu-1", "nodes": ["farm-ui-1"], "flags": ["--cpu"], "env": {}}}))
            await r.first_start_step()
            assert ("restart", "ui-1") in fake_agent.calls
            inst = [i for i in (await c.get("/api/admin/instances", headers=H)).json() if i["instance"]][0]
            assert inst["profile"] == "rocm-x-1-ui" and inst["instance"]["restart_backend"] == "http-agent"
            assert inst["instance"]["paths"]["input"] == str(tmp_path / "shared" / "input")
            # an administrator removes it: it is not brought back
            assert (await c.delete(f"/api/admin/workers/{inst['worker_id']}", headers=H)).status_code == 200
            await r.first_start_step()
            assert r.first_start["state"] == "off" and not (await c.get("/api/admin/workers", headers=H)).json()


# ---- the UI worker container: its supervisor ------------------------------------------------------------------
def test_the_supervisor_restarts_like_the_hosts_unit(tmp_path):
    """Restart=on-failure, but exit 2 (the launcher refused the render) stays down for the router to read."""
    code = tmp_path / "code"
    code.write_text("1")
    script = f"import sys,time; c=int(open({str(code)!r}).read()); time.sleep(0.2); sys.exit(c)"
    sup = uiworker.Supervisor([sys.executable, "-c", script], dict(os.environ), restart_sec=0.1)
    sup.start()
    deadline = time.time() + 10
    while sup.restarts < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sup.restarts >= 2                                         # a crash comes back by itself
    code.write_text("2")
    while sup.status()["ExecMainStatus"] != "2" and time.time() < deadline:
        time.sleep(0.05)
    n = sup.restarts
    time.sleep(0.6)
    assert sup.status() == {"ActiveState": "failed", "ExecMainStatus": "2", "NRestarts": str(n)}   # stays down
    code.write_text("0")
    sup.restart()                                                    # the router's start
    assert sup.status()["ActiveState"] == "active"
    sup.stop()


def test_the_supervisor_agent_serves_only_its_instance(tmp_path):
    sup = uiworker.Supervisor([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    do = uiworker.make_do(sup, {"instance": "ui-1", "etc": str(tmp_path)}, {})
    with pytest.raises(agent.Refused):
        do("restart", "gpu-1")
    with pytest.raises(agent.Refused):
        do("build", "ui-1")
    assert json.loads(do("status", "ui-1")[1])["ActiveState"] == "inactive"
    assert do("restart", "ui-1") == (0, "restart requested")
    deadline = time.time() + 5
    while sup.status()["ActiveState"] != "active" and time.time() < deadline:
        time.sleep(0.05)
    assert sup.status()["ActiveState"] == "active"
    sup.stop()


def test_the_container_layout_and_its_allow_list(tmp_path, monkeypatch):
    env = {"COMFYFARM_ROUTER": "http://router:8188", "COMFYFARM_ARTIFACTS": "/srv/comfyfarm/art",
           "COMFYFARM_SHARED": str(tmp_path / "shared"), "COMFYFARM_UI_HOME": str(tmp_path / "home")}
    lay = uiworker.prepare(env)
    assert json.loads((tmp_path / "home/etc/ui-1.check.json").read_text())["container"] is True
    monkeypatch.setattr(uiworker, "own_networks", lambda: ["172.30.0.0/16"])
    le = uiworker.launcher_env(env, lay)
    assert le["COMFY_LAUNCH_INSTANCE"] == "ui-1" and le["COMFYFARM_ALLOW_NETWORK"] == "172.30.0.0/16"
    assert le["COMFY_LAUNCH_PREFIXES"].split(",")[0] == str(tmp_path / "shared/input") and le["USER"] == "comfyfarm"
    assert le["COMFY_LAUNCH_ARTIFACTS"] == "/srv/comfyfarm/art" and le["COMFY_LAUNCH_LOCAL"] == str(tmp_path / "home/local")


async def test_the_setup_form_gets_the_deployments_own_defaults(dsn, tmp_path):
    """QA (newcomer path): the setup form offered /mnt/farm/... for the shared directories while compose put them under
    /srv/comfyfarm/shared, and left the router address empty — on one machine the UI worker and the GPU worker then
    used different storage. The router says what the deployment knows."""
    app = create_app(fast_settings(dsn, artifacts_dir="/srv/comfyfarm/art", ui_shared="/srv/comfyfarm/shared",
                                   public_router="192.0.2.10:18188"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            d = (await c.get("/api/admin/defaults", headers=H)).json()
            assert d == {"art": "/srv/comfyfarm/art", "shared": "/srv/comfyfarm/shared", "router": "192.0.2.10:18188"}
            assert (await c.get("/api/admin/defaults", headers={"X-ComfyFarm-User": "mallory"})).status_code == 403


async def test_getting_started_follows_the_first_day(dsn, fakes):
    """QA (newcomer path): the dashboard says where a new farm stands — UI worker, a build for the GPU, a GPU worker
    running, an artist account, a first job — and what is next."""
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            steps = lambda g: {s["key"]: s["done"] for s in g["steps"]}                     # noqa: E731
            g = (await c.get("/api/admin/getting-started", headers=H)).json()
            assert [s["key"] for s in g["steps"]] == ["ui", "build", "gpu", "user", "job"] and not any(steps(g).values())
            await c.post("/api/admin/profiles", json={"name": "ui-1", "core": "c", "venv": "v", "flags": ["--cpu"]}, headers=H)
            assert not steps((await c.get("/api/admin/getting-started", headers=H)).json())["build"]   # a CPU profile only
            await c.post("/api/admin/profiles", json={"name": "gpu-1", "core": "c", "venv": "v"}, headers=H)
            await c.post("/api/admin/groups", json={"name": "default"}, headers=H)
            async with r.db.conn() as conn:
                gid = await conn.fetchval("SELECT id FROM worker_groups")
                await conn.execute("INSERT INTO users(username, password_hash) VALUES ('artist', 'x')")
                await conn.execute("INSERT INTO user_groups(username, group_id) VALUES ('artist', $1)", gid)
            g = steps((await c.get("/api/admin/getting-started", headers=H)).json())
            assert g["build"] and g["user"] and not g["gpu"] and not g["job"]
            assert (await c.get("/api/admin/getting-started", headers={"X-ComfyFarm-User": "artist"})).status_code == 403


def test_the_builder_says_what_each_recipe_is_for(box):
    b, tmp, _ = box
    (tmp / "share/recipes/rocm-x.json").write_text(json.dumps({"recipe": "rocm-x", "about": "AMD RDNA4"}))
    p = b.policy()
    assert p["recipes"] == ["rocm-x"] and p["about"] == {"rocm-x": "AMD RDNA4"}
