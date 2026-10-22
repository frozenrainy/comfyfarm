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
        running = [json.load(open(f)) for f in (tmp_path / "status").glob("*.json") if builder.REQUEST.match(f.name[:-5])]
        assert any(x["state"] == "running" and x["recipe"] == "rocm-x" for x in running)     # shown while it builds
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
                     ({"recipe": "rocm-x", "nodes": [{**good, "post_install": "curl | sh"}]}, "node pack: only"),
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
            assert workers[0]["builtin"]          # compose sets it up: no "Set up again" (QA: a dead end)
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
                                   public_router="192.0.2.10:18188", models_dir="/srv/comfyfarm/model-store"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            d = (await c.get("/api/admin/defaults", headers=H)).json()
            assert d == {"art": "/srv/comfyfarm/art", "models": "/srv/comfyfarm/model-store", "shared": "/srv/comfyfarm/shared",
                         "router": "192.0.2.10:18188"}
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


def test_device_names_read_like_the_card():
    """QA (dev.11): the GPU column showed torch's device string ("cuda:0 AMD Radeon Graphics : native")."""
    from comfyfarm.api.admin import device_name
    assert device_name("cuda:0 AMD Radeon Graphics : native") == "AMD Radeon Graphics"
    assert device_name("cuda:1 NVIDIA GeForce RTX 3080 Ti : cudaMallocAsync") == "NVIDIA GeForce RTX 3080 Ti"
    assert device_name("Some Card") == "Some Card" and device_name(None) is None


def test_the_builder_signs_what_it_publishes_for_copies(tmp_path):
    """docs/design/16: GPU hosts accept a copy only with the builder's signature over its list of files. The key is the
    builder's own (made once); the store's older artifacts are signed too, and nothing twice."""
    from comfyfarm import fetch
    from test_fetch import published
    for d in ("requests", "status", "art", "share/recipes"):
        (tmp_path / d).mkdir(parents=True)
    key = str(tmp_path / "home" / "signing" / "artifacts")
    b = builder.Builder(str(tmp_path / "requests"), str(tmp_path / "status"), str(tmp_path / "art"), str(tmp_path / "share"),
                        None, [], key=key)
    assert b.policy()["signing_key"] is None and b.sign_store() == 0          # no key yet: nothing signed
    b.ensure_key()
    assert b.policy()["signing_key"].startswith("ssh-ed25519 ") and oct(os.stat(os.path.dirname(key)).st_mode & 0o777) == "0o700"
    published(tmp_path / "art", "venv", "v-1")
    published(tmp_path / "art", "core", "c-1")
    (tmp_path / "art" / "nodes" / "half-done").mkdir(parents=True)          # no READY: not published, not signed
    assert b.sign_store() == 2 and b.sign_store() == 0
    signers = tmp_path / "allowed"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {b.policy()["signing_key"]}\n')
    lst = tmp_path / "art" / "signed" / "venv" / "v-1.json"
    fetch.verify(lst.read_bytes(), (tmp_path / "art" / "signed" / "venv" / "v-1.json.sig").read_bytes(), str(signers))
    assert lst.read_bytes() == fetch.file_list(str(tmp_path / "art" / "venv" / "v-1"), "venv", "v-1")
    assert not (tmp_path / "art" / "signed" / "nodes").exists()


async def test_the_router_hands_on_signed_artifacts_to_its_hosts_only(farm, tmp_path):
    """docs/design/16: a host of the farm gets the builder's signed list and the tree; the copy it makes from them
    passes every check. Anyone else gets 403; an artifact not signed yet, 404 with the reason."""
    import io
    from comfyfarm import fetch
    from conftest import add_worker
    from test_fetch import published
    r, c, builds, art = farm
    published(art, "venv", "v-1")
    _, wid = await add_worker(r, "http://192.0.2.21:8188", name="gpu-a")
    async with r.db.conn() as x:
        await x.execute("INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, "
                        "restart_backend, restart_config) VALUES ($1, 'gpu-a', 'gpu', 'posix', $2, '0.0.0.0', 8188, "
                        "'{}'::jsonb, 'manual', '{}'::jsonb)", wid, ["127.0.0.1"])
    assert (await c.get("/api/worker_artifacts/venv/v-1/signed")).status_code == 404          # not signed yet
    key = str(tmp_path / "bkey" / "artifacts")
    b = builder.Builder(str(builds / "requests"), str(builds / "status"), str(art), str(tmp_path), None, [], key=key)
    b.ensure_key()
    assert b.sign_store() == 1
    got = (await c.get("/api/worker_artifacts/venv/v-1/signed")).json()
    stream = await c.get("/api/worker_artifacts/venv/v-1/tar")
    assert stream.status_code == 200 and stream.headers["content-type"] == "application/x-tar"
    signers = tmp_path / "allowed"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {b.public_key()}\n')
    fetch.verify(got["list"].encode(), got["sig"].encode(), str(signers))
    host_art = tmp_path / "host-art"
    fetch.install(io.BytesIO(stream.content), str(host_art), "venv", "v-1", got["list"].encode(), got["sig"].encode())
    assert fetch.file_list(str(host_art / "venv" / "v-1"), "venv", "v-1") == got["list"].encode()
    for bad in ("/api/worker_artifacts/venv/nope/tar", "/api/worker_artifacts/etc/passwd/tar",
                "/api/worker_artifacts/venv/..%2F..%2Fstate/signed"):
        assert (await c.get(bad)).status_code == 404, bad
    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=c._transport.app, client=("192.0.2.99", 4000)),
                                 base_url="http://farm")
    async with stranger:
        assert (await stranger.get("/api/worker_artifacts/venv/v-1/tar")).status_code == 403
        assert (await stranger.get("/api/worker_artifacts/venv/v-1/signed")).status_code == 403


def test_a_builder_that_cannot_make_its_key_still_builds(tmp_path, monkeypatch, capsys):
    """Field (E2E, dev.14): ssh-keygen refused to run for the builder's uid without a passwd entry, and the builder
    crashed in a loop. Builds go on without a key; only copies on GPU hosts wait for one."""
    b = builder.Builder(str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), None, [],
                        key=str(tmp_path / "signing" / "artifacts"))
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 255, "", "No user exists for uid 10002"))
    b.ensure_key()
    assert "copies on GPU hosts are off" in capsys.readouterr().out and b.public_key() is None


def test_a_new_builder_key_signs_the_store_again(tmp_path):
    """Field (dev.15): the control plane's data directory was deleted and started anew while the artifact store stayed.
    The new builder key must not leave the old key's signatures in place — hosts that pin the new key would refuse
    every copy. What another key signed is signed again (the lists are kept: artifacts never change)."""
    from comfyfarm import fetch
    from test_fetch import published
    for d in ("requests", "status", "art"):
        (tmp_path / d).mkdir(parents=True)
    published(tmp_path / "art", "venv", "v-1")
    mk = lambda home: builder.Builder(str(tmp_path / "requests"), str(tmp_path / "status"), str(tmp_path / "art"),  # noqa: E731
                                      str(tmp_path), None, [], key=str(tmp_path / home / "signing" / "artifacts"))
    old = mk("old-home")
    old.ensure_key()
    assert old.sign_store() == 1
    lst = tmp_path / "art" / "signed" / "venv" / "v-1.json"
    before = lst.read_bytes()
    new = mk("new-home")                                 # the builder's home was deleted: a new key
    new.ensure_key()
    assert new.sign_store() == 1 and new.sign_store() == 0
    signers = tmp_path / "pinned"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {new.public_key()}\n')
    fetch.verify(lst.read_bytes(), (tmp_path / "art" / "signed" / "venv" / "v-1.json.sig").read_bytes(), str(signers))
    assert lst.read_bytes() == before                    # the list itself was not made again


class FakeHF:
    """Hugging Face's revision API and resolve URLs for one repository, with ranges; `gated` answers 403."""

    def __init__(self, files: dict, commit="c" * 40, gated=False, lie=None, gated_files=False):
        import hashlib
        import http.server
        self.asked = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                outer.asked.append((h.path, h.headers.get("Range"), h.headers.get("Authorization")))
                if gated:
                    h.send_response(403); h.end_headers(); return
                if h.path.startswith("/api/models/org/repo/revision/"):
                    body = json.dumps({"sha": commit, "siblings": [
                        {"rfilename": p, "size": len(d), "lfs": {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}}
                        for p, d in files.items()]}).encode()
                    h.send_response(200); h.send_header("Content-Length", str(len(body))); h.end_headers()
                    h.wfile.write(body); return
                prefix = f"/org/repo/resolve/{commit}/"
                d = files.get(h.path[len(prefix):]) if h.path.startswith(prefix) else None
                if gated_files and d is not None and not h.headers.get("Authorization"):
                    h.send_response(401); h.end_headers(); return    # its files listed to anyone, served to members
                if d is None:
                    h.send_response(404); h.end_headers(); return
                d = lie if lie is not None else d
                start = int(h.headers["Range"].split("=")[1].split("-")[0]) if h.headers.get("Range") else 0
                h.send_response(206 if start else 200); h.send_header("Content-Length", str(len(d) - start)); h.end_headers()
                h.wfile.write(d[start:])

            def log_message(h, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        self.srv.shutdown()


def model_builder(tmp_path, hf, token=None, allow_pickle=False):
    for d in ("requests", "status", "art", "models"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    tok = None
    if token:
        tok = tmp_path / "hf_token"
        tok.write_text(token + "\n")
    b = builder.Builder(str(tmp_path / "requests"), str(tmp_path / "status"), str(tmp_path / "art"), str(tmp_path), None, [],
                        key=str(tmp_path / "home" / "signing" / "artifacts"), models=str(tmp_path / "models"),
                        hf_endpoint=hf.url, hf_token_file=str(tok) if tok else None, allow_pickle=allow_pickle)
    b.ensure_key()
    return b


def test_the_builder_downloads_a_model_pinned_checked_and_signed(tmp_path):
    """docs/design/17: a Hugging Face file into the model store — pinned to the commit its ref names, checked against
    the SHA-256 Hugging Face publishes, read-only, its entry signed with the builder's key; a token goes to Hugging Face
    only; an interrupted download goes on."""
    from comfyfarm import fetch
    data = os.urandom(300_000)
    hf = FakeHF({"split_files/model.safetensors": data})
    try:
        b = model_builder(tmp_path, hf, token="hf_secret")
        (tmp_path / "models" / ".incoming").mkdir()
        part = tmp_path / "models" / ".incoming" / f"{'c' * 12}-{__import__('hashlib').sha256(b'split_files/model.safetensors').hexdigest()[:12]}.part"
        part.write_bytes(data[:100_000])                                          # an earlier try broke off
        st = b.handle(ask(tmp_path, {"kind": "model", "repo": "org/repo", "path": "split_files/model.safetensors",
                                     "ref": "main", "folder": "diffusion_models", "filename": "model.safetensors"}))
        assert st["state"] == "done" and st["revision"] == "c" * 40 and st["size"] == 300_000, st
        dest = tmp_path / "models" / "diffusion_models" / "model.safetensors"
        assert dest.read_bytes() == data and (not os.access(dest, os.W_OK) or os.getuid() == 0)
        assert any(r == "bytes=100000-" for _, r, _ in hf.asked)                    # only the rest was downloaded
        assert all(a == "Bearer hf_secret" for _, _, a in hf.asked)                 # the token, to Hugging Face only
        assert "hf_secret" not in json.dumps(st)
        signers = tmp_path / "pinned"
        signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {b.public_key()}\n')
        lst = tmp_path / "models" / "signed" / "model" / f"{st['key']}.json"
        fetch.verify(lst.read_bytes(), (tmp_path / "models" / "signed" / "model" / f"{st['key']}.json.sig").read_bytes(),
                     str(signers))
        assert fetch.model_present(str(tmp_path / "models"), st["key"])
    finally:
        hf.stop()


@pytest.mark.parametrize("case", ["pickle", "gated", "gated files", "not what was published", "no such file", "escape"])
def test_a_model_the_builder_will_not_have(tmp_path, case):
    data = os.urandom(5000)
    hf = FakeHF({"m.safetensors": data, "m.ckpt": data}, gated=case == "gated", gated_files=case == "gated files",
                lie=os.urandom(5000) if case == "not what was published" else None)
    try:
        b = model_builder(tmp_path, hf)
        req = {"kind": "model", "repo": "org/repo", "path": "m.safetensors", "folder": "checkpoints", "filename": "m.safetensors"}
        if case == "pickle":
            req.update(path="m.ckpt", filename="m.ckpt")
        if case == "no such file":
            req.update(path="other.safetensors")
        if case == "escape":
            req.update(filename="../../../etc/x.safetensors")
        st = b.handle(ask(tmp_path, req))
        want = {"pickle": "can run code", "gated": "gated or private", "gated files": "gated or private",
                "not what was published": "thrown away",
                "no such file": "no file", "escape": "bad place"}[case]
        assert st["state"] == "failed" and want in st["error"], st
        assert not list((tmp_path / "models").glob("*/*.safetensors")) and not list((tmp_path / "models").glob("*/*.ckpt"))
    finally:
        hf.stop()


def test_a_model_in_the_store_already_is_not_downloaded_again(tmp_path):
    """Added again (a database started anew keeps the store): checked against Hugging Face, signed, nothing fetched.
    Another revision of the file at the same place replaces it, and the old entry goes with it."""
    from comfyfarm import fetch
    data = os.urandom(20_000)
    hf = FakeHF({"m.safetensors": data})
    req = {"kind": "model", "repo": "org/repo", "path": "m.safetensors", "folder": "loras", "filename": "m.safetensors"}
    try:
        b = model_builder(tmp_path, hf)
        first = b.handle(ask(tmp_path, req))
        n = len(hf.asked)
        again = b.handle(ask(tmp_path, req))
        assert again["state"] == "done" and again["key"] == first["key"]
        assert [p for p, _, _ in hf.asked[n:]] == [p for p, _, _ in hf.asked[:1]]    # the revision API only
    finally:
        hf.stop()
    hf = FakeHF({"m.safetensors": os.urandom(20_000)}, commit="d" * 40)
    try:
        b.hf_endpoint = hf.url
        newer = b.handle(ask(tmp_path, req))
        assert newer["state"] == "done" and newer["key"] != first["key"]
        assert fetch.model_present(str(tmp_path / "models"), newer["key"])
        assert not os.path.exists(tmp_path / "models" / "signed" / "model" / f"{first['key']}.json")
    finally:
        hf.stop()
