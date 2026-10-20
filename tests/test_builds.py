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
    os.mkdir(os.path.join(box["req"], "a-directory"))                      # the drop box is group-writable: dirs too
    os.symlink("/etc/passwd", os.path.join(box["req"], "a-link"))
    assert build.status("rocm-gfx1201", box["st"], box["req"])["state"] == "queued"      # not the last run's state
    assert "already queued" in build.request("rocm-gfx1201", box["req"], box["here"])
    done = build.run("/srv/art", "/usr/local/bin/uv", box["req"], box["st"], box["here"], as_user=None, runner=runner)
    assert done == ["rocm-gfx1201"] and len(ran) == 1 and os.listdir(box["req"]) == []   # junk removed, never run
    assert ran[0][2:] == ["recipe", "/srv/art", "rocm-gfx1201", "--uv", "/usr/local/bin/uv", "--recipes", box["here"]]
    st = build.status("rocm-gfx1201", box["st"], box["req"])
    assert st["state"] == "done" and st["template"] == "rocm-gfx1201-1" and "published" in st["log"]
    assert build.status("other-recipe", box["st"], box["req"])["state"] == "none"       # nothing written for junk
    assert build.status("never", box["st"], box["req"]) == {"recipe": "never", "state": "none"}
    assert os.path.exists("/etc/passwd")                                                   # the link's target untouched


def test_root_never_writes_through_a_link_in_the_status_directory(box, tmp_path):
    """status/ is root's; still, a link there (whoever made it) is never followed by the root writer."""
    target = tmp_path / "unit.service"
    target.write_text("ExecStart=/bin/true\n")
    os.symlink(str(target), os.path.join(box["st"], "rocm-gfx1201.json.tmp"))
    build.request("rocm-gfx1201", box["req"], box["here"])
    with pytest.raises(OSError):
        build.run("/srv/art", "uv", box["req"], box["st"], box["here"], as_user=None,
                  runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"{}"))
    assert target.read_text() == "ExecStart=/bin/true\n"
    os.unlink(os.path.join(box["st"], "rocm-gfx1201.json.tmp"))
    os.symlink(str(target), os.path.join(box["st"], "rocm-gfx1201.log"))
    build.request("rocm-gfx1201", box["req"], box["here"])
    with pytest.raises(OSError):
        build.run("/srv/art", "uv", box["req"], box["st"], box["here"], as_user=None,
                  runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"{}"))
    assert target.read_text() == "ExecStart=/bin/true\n"


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
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "usr" / "bin" / "git").write_text("")
    return root


def uv_tarball(arch: str, binary: bytes = b"#!/bin/sh\necho uv\n") -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in ((f"uv-{arch}-unknown-linux-gnu/uv", binary), (f"uv-{arch}-unknown-linux-gnu/uvx", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def pinned_uv(monkeypatch, request):
    """The pinned uv comes from a fake release: its checksum is pinned for this test, downloads are counted."""
    import hashlib
    if "shipped_uv_pin" in request.node.name:             # that one looks at the real pins
        return None
    import platform
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    blob = uv_tarball(arch)
    monkeypatch.setattr(ws, "UV_SHA256", {arch: hashlib.sha256(blob).hexdigest()})
    calls = []
    monkeypatch.setattr(ws, "fetch_url", lambda url, timeout=120: calls.append(url) or blob)
    return {"arch": arch, "calls": calls, "blob": blob}


def test_setup_with_builds_installs_the_builder_and_without_removes_its_units(tmp_path):
    root = builder_root(tmp_path)
    rec = Recorder()
    res = ws.setup(build_payload(), ws.Host(root=str(root), run=rec))
    assert res["ok"], res
    tools = root / "opt" / "comfyfarm" / "worker"
    assert (tools / "recipes" / "rocm-gfx1201.json").exists() and (tools / "ui_ext" / "comfyfarm_ui" / "web" / "x.js").exists()
    assert rec.ran("useradd")[-1][-1] == "comfyfarm-build"
    useradd = rec.ran("useradd")[-1]
    assert "--no-create-home" in useradd and useradd[useradd.index("--home-dir") + 1] == "/var/lib/comfyfarm-build/home"
    # root's directories: nothing root writes is in a place the build account can put a link
    assert oct(os.stat(root / "var/lib/comfyfarm-build").st_mode & 0o777) == "0o711"
    assert oct(os.stat(root / "var/lib/comfyfarm-build/status").st_mode & 0o777) == "0o755"
    assert oct(os.stat(root / "var/lib/comfyfarm-build/home").st_mode & 0o777) == "0o700"
    assert "COMFYFARM_AGENT_BUILD=1" in (root / "etc" / "comfyfarm" / "agent.env").read_text()
    svc = (root / "etc" / "systemd" / "system" / "comfyfarm-build.service").read_text()
    assert "build.py run /srv/art /opt/comfyfarm/worker/bin/uv" in svc
    uv = root / "opt" / "comfyfarm" / "worker" / "bin" / "uv"
    assert uv.read_bytes().startswith(b"#!/bin/sh") and oct(uv.stat().st_mode & 0o777) == "0o755"
    assert "DirectoryNotEmpty=/var/lib/comfyfarm-build/requests" in (root / "etc/systemd/system/comfyfarm-build.path").read_text()
    assert ["systemctl", "enable", "--now", "comfyfarm-build.path"] in rec.calls
    assert ["chgrp", "comfyfarm", "/var/lib/comfyfarm-build/requests"] in rec.calls
    assert ["chmod", "0711", "/var/lib/comfyfarm-build"] in rec.calls      # the service account passes through to the drop box
    # set up again without builds: the units go, the permission goes
    rec2 = Recorder()
    res = ws.setup(payload(), ws.Host(root=str(root), run=rec2))
    assert res["ok"], res
    assert not (root / "etc" / "systemd" / "system" / "comfyfarm-build.service").exists()
    assert "COMFYFARM_AGENT_BUILD" not in (root / "etc" / "comfyfarm" / "agent.env").read_text()
    assert ["systemctl", "disable", "--now", "comfyfarm-build.path"] in rec2.calls


def test_setup_with_builds_refuses_what_it_should(tmp_path):
    res = ws.setup(build_payload(), ws.Host(root=str(fake_root(tmp_path / "a")), run=Recorder()))   # no git
    assert not res["ok"] and "git" in res["error"]
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


def test_the_pinned_uv_is_checked_installed_once_and_never_taken_unverified(tmp_path, pinned_uv, monkeypatch):
    """Field: uv had to be put on every account's PATH by hand. Setup now brings the version pinned with this release
    — the release's checksum checked before anything is unpacked."""
    root = builder_root(tmp_path)
    assert ws.setup(build_payload(), ws.Host(root=str(root), run=Recorder()))["ok"]
    assert len(pinned_uv["calls"]) == 1 and f"/{ws.UV_VERSION}/uv-{pinned_uv['arch']}-unknown-linux-gnu.tar.gz" in pinned_uv["calls"][0]
    assert pinned_uv["calls"][0].startswith("https://github.com/astral-sh/uv/releases/download/")
    assert ws.setup(build_payload(), ws.Host(root=str(root), run=Recorder()))["ok"] and len(pinned_uv["calls"]) == 1   # once
    # another download (a tampered mirror, a new release under the same name): refused, nothing installed
    monkeypatch.setattr(ws, "fetch_url", lambda url, timeout=120: uv_tarball(pinned_uv["arch"], b"evil"))
    root2 = builder_root(tmp_path / "b")
    res = ws.setup(build_payload(), ws.Host(root=str(root2), run=Recorder()))
    assert not res["ok"] and "pinned checksum" in res["error"] and not (root2 / "opt/comfyfarm/worker/bin/uv").exists()

    def offline(url, timeout=120):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(ws, "fetch_url", offline)
    res = ws.setup(build_payload(), ws.Host(root=str(builder_root(tmp_path / "c")), run=Recorder()))
    assert not res["ok"] and "need internet" in res["error"]


def test_the_shipped_uv_pin_is_the_release_checksum_format():
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in ws.UV_SHA256.values()) and set(ws.UV_SHA256) == {"x86_64", "aarch64"}


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


def test_a_worker_set_up_again_with_another_account_takes_the_key_from_the_old_one(tmp_path, monkeypatch):
    import pwd
    root = fake_root(tmp_path)
    old = root / "var" / "lib" / "olduser" / ".ssh" / "authorized_keys"
    old.parent.mkdir(parents=True)
    old.write_text(f"{ws.MARK}\ncommand=... ssh-ed25519 AAAA router\n")
    real = pwd.getpwnam
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd(("olduser", "x", 1500, 1500, "", "/var/lib/olduser", "/bin/sh"))
                        if n == "olduser" else real(n))
    rec = Recorder()
    res = ws.setup(payload(previous_account="olduser"), ws.Host(root=str(root), run=rec))
    assert res["ok"], res
    assert not old.exists() and ["systemctl", "--user", "disable", "--now", "comfyfarm@gpu-1"] in [c[-5:] for c in rec.calls]


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
    again = await c.post("/api/admin/setups", json=spec(worker_id=w["id"], builds=True, account="farmsvc"), headers=H)
    assert again.status_code == 200, again.text
    s = (await c.get(f"/api/admin/setups/{again.json()['id']}", headers=H)).json()
    assert s["spec"]["previous_account"] == "comfyfarm" and s["extra"]["again"] == w["id"]   # the old account's key goes
    mine = (await c.get(f"/api/admin/setups?worker_id={w['id']}", headers=H)).json()
    assert [x["id"] for x in mine] == [] or all(x["worker_id"] == w["id"] for x in mine)       # only that worker's
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
