"""Builds from the dashboard (opt-in at setup): the router names an installed recipe, the host builds it as its build
account; and setting an existing worker up again."""
import base64
import json
import os
import subprocess
import sys

import pytest

from comfyfarm import agent
from comfyfarm import build
from comfyfarm import hostcheck
from comfyfarm import worker_setup as ws
from comfyfarm.core import hostsetup
from test_setup_api import H, farm, spec  # noqa: F401 — the fixture
from test_worker_setup import Recorder, fake_root, payload


@pytest.fixture
def box(tmp_path):
    here = tmp_path / "tools"
    (here / "recipes").mkdir(parents=True)
    (here / "recipes" / "rocm-gfx1201.json").write_text("{}")
    req, st = tmp_path / "requests", tmp_path / "status"
    req.mkdir()
    st.mkdir()
    return {"here": str(here), "req": str(req), "st": str(st)}


def test_only_an_installed_recipe_can_be_requested_and_only_it_is_built(box):
    assert "requested" in build.request("rocm-gfx1201", box["req"], box["here"])
    with pytest.raises(ValueError, match="installed: rocm-gfx1201"):
        build.request("evil", box["req"], box["here"])
    # what else lands in the drop box (the service account can write files there) is never run
    for junk in ("other-recipe", ".hidden", "x y"):
        open(os.path.join(box["req"], junk), "w").close()
    ran = []

    def runner(argv, **kw):
        ran.append(argv)
        kw["stderr"].write(b"venv/rocm72-1: published\n")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"template": "rocm-gfx1201-1"}).encode())
    done = build.run("/srv/art", "/usr/local/bin/uv", box["req"], box["st"], box["here"], as_user=None, runner=runner)
    assert done == ["rocm-gfx1201"] and len(ran) == 1 and os.listdir(box["req"]) == []
    assert ran[0][2:] == ["recipe", "/srv/art", "rocm-gfx1201", "--uv", "/usr/local/bin/uv", "--recipes", box["here"]]
    st = build.status("rocm-gfx1201", box["st"])
    assert st["state"] == "done" and st["template"] == "rocm-gfx1201-1" and "published" in st["log"]
    assert build.status("other-recipe", box["st"])["state"] == "failed"
    assert build.status("never", box["st"]) == {"recipe": "never", "state": "none"}


def test_a_failed_build_says_so(box):
    build.request("rocm-gfx1201", box["req"], box["here"])
    build.run("/srv/art", "uv", box["req"], box["st"], box["here"], as_user=None,
              runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout=b""))
    assert build.status("rocm-gfx1201", box["st"])["state"] == "failed"


def test_the_agent_builds_only_where_setup_allowed_it(box, monkeypatch, tmp_path):
    monkeypatch.setattr(build, "REQUESTS", box["req"])
    monkeypatch.setattr(build, "HERE", box["here"])
    monkeypatch.setenv("COMFYFARM_AGENT_INSTANCES", "gpu-1")
    monkeypatch.delenv("COMFYFARM_AGENT_BUILD", raising=False)
    monkeypatch.setattr(agent, "CONFIG", str(tmp_path / "none.env"))
    with pytest.raises(agent.Refused, match="not allowed"):
        agent.do("build", "rocm-gfx1201")
    monkeypatch.setenv("COMFYFARM_AGENT_BUILD", "1")
    assert agent.do("build", "rocm-gfx1201") == (0, "build of rocm-gfx1201 requested")
    with pytest.raises(agent.Refused, match="no recipe"):
        agent.do("build", "../etc")


def build_payload(**spec_kw):
    p = payload(builds=True, **spec_kw)
    p["build_files"] = {"publish.py": base64.b64encode(b"# publish\n").decode(),
                        "build.py": base64.b64encode(b"# build\n").decode(),
                        "recipes/rocm-gfx1201.json": base64.b64encode(b"{}").decode(),
                        "worker_ext/comfyfarm_worker/__init__.py": base64.b64encode(b"#\n").decode(),
                        "ui_ext/comfyfarm_ui/web/x.js": base64.b64encode(b"//\n").decode()}
    return p


def builder_root(tmp_path):
    root = fake_root(tmp_path)
    for tool in ("usr/local/bin/uv", "usr/bin/git"):
        (root / tool).parent.mkdir(parents=True, exist_ok=True)
        (root / tool).write_text("")
    return root


def test_setup_with_builds_installs_the_builder_and_without_removes_its_units(tmp_path):
    root = builder_root(tmp_path)
    rec = Recorder()
    res = ws.setup(build_payload(), ws.Host(root=str(root), run=rec))
    assert res["ok"], res
    tools = root / "opt" / "comfyfarm" / "worker"
    assert (tools / "recipes" / "rocm-gfx1201.json").exists() and (tools / "ui_ext" / "comfyfarm_ui" / "web" / "x.js").exists()
    assert rec.ran("useradd")[-1][-1] == "comfyfarm-build"
    assert "COMFYFARM_AGENT_BUILD=1" in (root / "etc" / "comfyfarm" / "agent.env").read_text()
    svc = (root / "etc" / "systemd" / "system" / "comfyfarm-build.service").read_text()
    assert "build.py run /srv/art /usr/local/bin/uv" in svc
    assert "DirectoryNotEmpty=/var/lib/comfyfarm-build/requests" in (root / "etc/systemd/system/comfyfarm-build.path").read_text()
    assert ["systemctl", "enable", "--now", "comfyfarm-build.path"] in rec.calls
    assert ["chgrp", "comfyfarm", "/var/lib/comfyfarm-build/requests"] in rec.calls
    assert ["chmod", "0711", "/var/lib/comfyfarm-build"] in rec.calls      # field: useradd's 0750 locked the drop box away
    # set up again without builds: the units go, the permission goes
    rec2 = Recorder()
    res = ws.setup(payload(), ws.Host(root=str(root), run=rec2))
    assert res["ok"], res
    assert not (root / "etc" / "systemd" / "system" / "comfyfarm-build.service").exists()
    assert "COMFYFARM_AGENT_BUILD" not in (root / "etc" / "comfyfarm" / "agent.env").read_text()
    assert ["systemctl", "disable", "--now", "comfyfarm-build.path"] in rec2.calls


def test_setup_with_builds_refuses_what_it_should(tmp_path):
    res = ws.setup(build_payload(), ws.Host(root=str(fake_root(tmp_path / "a")), run=Recorder()))   # no uv
    assert not res["ok"] and "uv" in res["error"]
    p = build_payload()
    p["build_files"]["../../etc/cron.d/x"] = base64.b64encode(b"* * * * * root sh").decode()
    res = ws.setup(p, ws.Host(root=str(builder_root(tmp_path / "b")), run=Recorder()))
    assert not res["ok"] and "unexpected file" in res["error"]
    root = builder_root(tmp_path / "c")                   # a store with contents that someone else owns
    (root / "srv" / "art" / "core").mkdir(parents=True)
    res = ws.setup(build_payload(), ws.Host(root=str(root), run=Recorder()))
    assert not res["ok"] and "chown -R comfyfarm-build /srv/art" in res["error"]
    root = builder_root(tmp_path / "d")                   # an empty one is handed over
    (root / "srv" / "art").mkdir(parents=True)
    rec = Recorder()
    res = ws.setup(build_payload(), ws.Host(root=str(root), run=rec))
    assert res["ok"] and ["chown", "comfyfarm-build", "/srv/art"] in rec.calls


def test_the_router_sends_build_tools_only_to_a_host_that_allows_builds():
    files = hostsetup.build_files()
    assert {"publish.py", "build.py", "recipes/rocm-gfx1201.json", "worker_ext/comfyfarm_worker/__init__.py"} <= set(files)
    assert all(ws.BUILD_FILE.match(k) for k in files), [k for k in files if not ws.BUILD_FILE.match(k)]
    for builds in (False, True):
        text, _ = hostsetup.build_script({"instance": "gpu-1", "builds": builds}, "http://r/x", "1")
        ns = {"__name__": "served"}
        exec(compile(text, "s.py", "exec"), ns)
        assert ("build_files" in ns["PAYLOAD"]) is builds


def test_the_host_check_names_the_recipes_a_build_may_use(tmp_path):
    root = tmp_path / "root"
    (root / "etc" / "comfyfarm").mkdir(parents=True)
    (root / "opt" / "comfyfarm" / "worker" / "recipes").mkdir(parents=True)
    (root / "opt" / "comfyfarm" / "worker" / "recipes" / "rocm-gfx1201.json").write_text("{}")
    (root / "etc" / "comfyfarm" / "agent.env").write_text("COMFYFARM_AGENT_BUILD=1\n")
    c = hostcheck.Check({}, {"instance": "x"}, root=str(root), cmd=lambda *a, **k: (127, ""), user="comfyfarm")
    c.builds()
    assert c.out == [{"level": "OK", "name": "builds", "detail": "from the dashboard: rocm-gfx1201 (built as comfyfarm-build)",
                      "recipes": ["rocm-gfx1201"]}]
    (root / "etc" / "comfyfarm" / "agent.env").write_text("COMFYFARM_AGENT_BUILD=0\n")
    c.out = []
    c.builds()
    assert c.out == []


async def test_setting_a_worker_up_again_keeps_it_and_build_through_the_api(farm, tmp_path, monkeypatch):
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(builds=True), headers=H)).json()
    assert made["id"]
    w = (await c.post("/api/admin/workers", json={"name": "gpu one", "base_url": "http://10.0.0.21:8188"}, headers=H)).json()
    (keys / "known_hosts").write_text("10.0.0.21 ssh-ed25519 AAAAhostkey\n")
    cfg = {"instance": "gpu-1", "hosts": ["10.0.0.21"], "port": 8188, "restart_backend": "ssh",
           "restart_config": {"host": "10.0.0.21", "user": "comfyfarm", "key": "comfyfarm-agent", "known_hosts": "known_hosts"},
           "paths": spec()["paths"]}
    assert (await c.put(f"/api/admin/instances/{w['id']}", json=cfg, headers=H)).status_code == 200
    # a new worker cannot take that instance or name; the same worker set up again can
    assert (await c.post("/api/admin/setups", json=spec(), headers=H)).status_code == 409
    again = await c.post("/api/admin/setups", json=spec(worker_id=w["id"], builds=True), headers=H)
    assert again.status_code == 200, again.text
    other = await c.post("/api/admin/setups", json=spec(worker_id=w["id"], name="renamed"), headers=H)
    assert other.status_code == 409 and "keeps its name" in other.text
    # build and its state through the agent (a fake ssh answering for it)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "ssh").write_text(f"""#!{sys.executable}
import sys
cmd = sys.argv[-1]
if cmd.startswith("build-status "):
    print('{{"recipe": "rocm-gfx1201", "state": "running", "log": "venv/rocm72-1: building"}}')
elif cmd.startswith("build "):
    print("build of rocm-gfx1201 requested")
else:
    sys.exit(255)
""")
    (bin_ / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    (keys / "comfyfarm-agent").write_text("key")
    got = await c.post(f"/api/admin/instances/{w['id']}/build", json={"recipe": "rocm-gfx1201"}, headers=H)
    assert got.status_code == 200 and got.json()["requested"]
    st = await c.get(f"/api/admin/instances/{w['id']}/build", params={"recipe": "rocm-gfx1201"}, headers=H)
    assert st.json()["state"] == "running" and "building" in st.json()["log"]
    bad = await c.post(f"/api/admin/instances/{w['id']}/build", json={"recipe": "-oProxyCommand=x"}, headers=H)
    assert bad.status_code == 502
