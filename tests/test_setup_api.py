"""Worker setup through the router (docs/design/14): create → the host fetches the script with its token → it runs
(here on a fake host) and reports → the dashboard adds the worker with the pinned host keys → the token is spent."""
import json
import os
import stat
import sys

import httpx
import pytest

from comfyfarm import worker_setup as ws
from comfyfarm.app import create_app
from conftest import fast_settings
from test_worker_setup import Recorder, fake_root

H = {"X-ComfyFarm-User": "admin"}


@pytest.fixture
async def farm(dsn, tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    app = create_app(fast_settings(dsn, keys_dir=str(keys)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield app.state.router, c, keys


def spec(**kw):
    base = {"name": "gpu one", "host": "10.0.0.21", "instance": "gpu-1", "gpu": "amd", "router_url": "http://10.0.0.5:8188",
            "paths": {"art": "/srv/art", "local": "/var/lib/comfyfarm/local", "models": "/srv/models",
                      "input": "/mnt/farm/input", "output": "/mnt/farm/output", "user": "/mnt/farm/user",
                      "temp": "/var/lib/comfyfarm/temp/gpu-1"},
            "mounts": [{"source": "nas:/export/farm", "target": "/mnt/farm"}]}
    base.update(kw)
    return base


def run_served_script(text: str, root) -> dict:
    """Executes the script the router served, as the host would — on a fake host."""
    ns = {"__name__": "served"}
    exec(compile(text, "comfyfarm-setup.py", "exec"), ns)
    assert ns["PAYLOAD"]["spec"]["instance"] == "gpu-1"
    return ws.setup(ns["PAYLOAD"], ws.Host(root=str(root), run=Recorder()))


async def test_setup_from_command_to_worker(farm, tmp_path):
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(), headers=H)).json()
    assert made["script_url"].startswith("http://10.0.0.5:8188/api/worker_setup/")
    assert made["script_sha256"] in made["command"] and "sudo python3 /tmp/comfyfarm-setup.py" in made["command"]
    assert (keys / "comfyfarm-agent").exists() and (keys / "comfyfarm-agent.pub").exists()
    path = "/" + made["script_url"].split("/", 3)[3]
    got = await c.get(path)                                       # no user: the token is the credential
    import hashlib
    assert got.status_code == 200 and hashlib.sha256(got.content).hexdigest() == made["script_sha256"]
    res = run_served_script(got.text, fake_root(tmp_path))
    assert res["ok"], res
    assert "comfyfarm-router" in (fake_root_ak := (tmp_path / "root/var/lib/comfyfarm/.ssh/authorized_keys").read_text())
    assert (keys / "comfyfarm-agent.pub").read_text().split()[1] in fake_root_ak
    rep = await c.post(path.replace("/script", "/report"), content=json.dumps(res))
    assert rep.status_code == 200
    s = (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()
    assert s["status"] == "reported" and s["result"]["pinned_host_keys"] == 1
    assert (keys / "known_hosts").read_text() == "10.0.0.21 ssh-ed25519 AAAAhostkey\n"
    # the dashboard adds the worker with what the setup computed; the instance passes the usual checks
    w = (await c.post("/api/admin/workers", json=s["worker"], headers=H)).json()
    put = await c.put(f"/api/admin/instances/{w['id']}", json=s["instance_config"], headers=H)
    assert put.status_code == 200, put.text
    cfg = s["instance_config"]["restart_config"]
    assert cfg == {"host": "10.0.0.21", "user": "comfyfarm", "key": "comfyfarm-agent", "known_hosts": "known_hosts"}
    assert (await c.post(f"/api/admin/setups/{made['id']}/finished?worker_id={w['id']}", headers=H)).status_code == 200
    assert (await c.get(path)).status_code == 404                                  # the token is spent


async def test_a_failed_host_check_is_kept_and_can_be_rerun(farm, tmp_path):
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(), headers=H)).json()
    path = "/" + made["script_url"].split("/", 3)[3]
    bad = {"ok": False, "check": [{"level": "FAIL", "name": "GPU", "detail": "none usable"}],
           "host_keys": ["ssh-ed25519 AAAAhostkey"]}
    await c.post(path.replace("/script", "/report"), content=json.dumps(bad))
    s = (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()
    assert s["status"] == "failed" and "instance_config" not in s
    assert (await c.post(f"/api/admin/setups/{made['id']}/finished?worker_id=1", headers=H)).status_code == 409
    assert (await c.get(path)).status_code == 200                                  # fix the host, run it again
    await c.post(path.replace("/script", "/report"), content=json.dumps(dict(bad, ok=True, check=[])))
    assert (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()["status"] == "reported"
    no_keys = await c.post(path.replace("/script", "/report"), content=json.dumps({"ok": True, "host_keys": []}))
    assert no_keys.status_code == 200
    assert (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()["status"] == "failed"   # nothing to pin


async def test_setup_input_is_checked(farm):
    r, c, keys = farm
    assert (await c.post("/api/admin/setups", json=spec(), headers={"X-ComfyFarm-User": "ann"})).status_code == 403
    for bad in [spec(instance="../x"), spec(host="-oProxyCommand=x"), spec(account="root"), spec(gpu="tpu"),
                spec(router_url="file:///etc"), spec(paths=dict(spec()["paths"], input="/var/lib/comfyfarm/local/nodes/x")),
                spec(mounts=[{"source": "nas:/x", "target": "rel"}]), spec(paths=dict(spec()["paths"], art="art"))]:
        resp = await c.post("/api/admin/setups", json=bad, headers=H)
        assert resp.status_code == 400, (bad, resp.text)
    assert (await c.get("/api/worker_setup/not-a-token/script")).status_code == 404
    assert (await c.post("/api/worker_setup/not-a-token/report", content="{}")).status_code == 404


async def test_expired_tokens_stop_working(farm):
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(), headers=H)).json()
    async with r.db.conn() as x:
        await x.execute("UPDATE worker_setups SET expires_at = now() - interval '1 minute'")
    assert (await c.get("/" + made["script_url"].split("/", 3)[3])).status_code == 404


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """An `ssh` that records its arguments and environment: the first call (upload) stores stdin and prints the
    remote path, the second (run) prints what sudo would have read and the script's output."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    rec = tmp_path / "ssh-calls.jsonl"
    (bin_ / "ssh").write_text(f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
data = sys.stdin.buffer.read()
with open({str(rec)!r}, "a") as f:
    f.write(json.dumps({{"argv": argv, "askpass": os.environ.get("SSH_ASKPASS"), "has_pw_env": bool(os.environ.get("COMFYFARM_SETUP_PASSWORD")),
                        "stdin_head": data[:40].decode(errors="replace")}}) + "\\n")
if "cat >" in argv[-1]:
    print("/tmp/comfyfarm-setup.Ab12Cd")
else:
    print("sudo read: " + data.decode().splitlines()[0])
    print("- preconditions")
""")
    (bin_ / "ssh").chmod((bin_ / "ssh").stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return rec


async def test_router_runs_setup_over_ssh_without_keeping_the_password(farm, fake_ssh):
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(), headers=H)).json()
    assert (await c.post(f"/api/admin/setups/{made['id']}/ssh", json={"user": "-oProxyCommand", "password": "x"},
                         headers=H)).status_code == 400
    resp = await c.post(f"/api/admin/setups/{made['id']}/ssh", json={"user": "ops", "password": "s3cret-pw"}, headers=H)
    assert resp.status_code == 200
    import asyncio
    for _ in range(100):
        s = (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()
        if "- preconditions" in s["log"]:
            break
        await asyncio.sleep(0.05)
    calls = [json.loads(line) for line in fake_ssh.read_text().splitlines()]
    assert len(calls) == 2 and all("s3cret-pw" not in " ".join(x["argv"]) for x in calls)        # never on a command line
    assert all(x["askpass"] and x["has_pw_env"] for x in calls)
    assert calls[0]["stdin_head"].startswith('"""Worker setup')                                    # the script, uploaded
    assert "sudo -S -p ''" in calls[1]["argv"][-1] and "-l" in calls[1]["argv"] and "ops" in calls[1]["argv"]
    assert "s3cret-pw" not in s["log"] and "sudo read: ********" in s["log"]                       # masked in the log
    async with r.db.conn() as x:
        dump = json.dumps([dict(row) for row in await x.fetch("SELECT spec, extra, log, result FROM worker_setups")], default=str)
        audit_dump = json.dumps([dict(row) for row in await x.fetch("SELECT detail FROM audit_log")], default=str)
    assert "s3cret-pw" not in dump and "s3cret-pw" not in audit_dump


async def test_host_check_through_the_agent_in_dashboard_and_doctor(farm, tmp_path, monkeypatch):
    """`check` over the instance's ssh backend (here a fake ssh answering for the agent): the dashboard shows the rows,
    doctor summarises them per instance; an agent that cannot answer is said so, not hidden."""
    r, c, keys = farm
    (keys / "comfyfarm-agent").write_text("key")
    (keys / "known_hosts").write_text("10.0.0.21 ssh-ed25519 AAAAhostkey\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps([{"level": "OK", "name": "router", "detail": "answers"},
                                  {"level": "FAIL", "name": "GPU", "detail": "setup expects an AMD GPU and none is usable here"}]))
    (bin_ / "ssh").write_text(f"#!/bin/sh\ncase \"$*\" in *check*) cat {answer};; *) exit 255;; esac\n")
    (bin_ / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    w = (await c.post("/api/admin/workers", json={"name": "gpu one", "base_url": "http://10.0.0.21:8188"}, headers=H)).json()
    cfg = {"instance": "gpu-1", "hosts": ["10.0.0.21"], "port": 8188, "restart_backend": "ssh",
           "restart_config": {"host": "10.0.0.21", "user": "comfyfarm", "key": "comfyfarm-agent", "known_hosts": "known_hosts"},
           "paths": spec()["paths"]}
    assert (await c.put(f"/api/admin/instances/{w['id']}", json=cfg, headers=H)).status_code == 200
    got = (await c.post(f"/api/admin/instances/{w['id']}/check", headers=H)).json()
    assert [x["level"] for x in got["check"]] == ["OK", "FAIL"]
    from comfyfarm import doctor
    lines = await doctor.run(fast_settings(r.s.dsn, keys_dir=str(keys)))
    host = [x for x in lines if x[1] == "host gpu-1"]
    assert host and host[0][0] == "FAIL" and "GPU" in host[0][2]
    answer.write_text("refused: instance 'gpu-1' is not served here")             # the agent could not run it
    resp = await c.post(f"/api/admin/instances/{w['id']}/check", headers=H)
    assert resp.status_code == 502 and "did not run" in resp.text


async def test_a_ui_worker_is_set_up_the_same_way_and_never_in_a_group(farm, tmp_path):
    r, c, keys = farm
    assert (await c.post("/api/admin/setups", json=spec(role="ui", group="amd"), headers=H)).status_code == 400
    made = (await c.post("/api/admin/setups", json=spec(role="ui", name="ui one", instance="ui-1", port=8189, gpu="amd"),
                         headers=H)).json()
    path = "/" + made["script_url"].split("/", 3)[3]
    await c.post(path.replace("/script", "/report"), content=json.dumps({"ok": True, "host_keys": ["ssh-ed25519 AAAAhostkey"]}))
    s = (await c.get(f"/api/admin/setups/{made['id']}", headers=H)).json()
    assert s["spec"]["gpu"] == "none" and s["instance_config"]["role"] == "ui" and s["worker"]["kind"] == "ui"
    w = (await c.post("/api/admin/workers", json=s["worker"], headers=H)).json()
    await c.post("/api/admin/groups", json={"name": "g1", "requirements": {}}, headers=H)
    bad = await c.patch(f"/api/admin/workers/{w['id']}", json={"group": "g1"}, headers=H)
    assert bad.status_code == 400 and "no group" in bad.text
    listed = (await c.get("/api/admin/workers", headers=H)).json()
    me = next(x for x in listed if x["id"] == w["id"])
    assert me["kind"] == "ui" and not any("no group" in x for x in me["warnings"])


async def test_an_unfinished_setup_gets_a_new_command(farm):
    """Doc-only rehearsal: the dialog was closed while the host ran the script, and the setup could not be reached again.
    Only the token's hash is kept, so a new command is made; the old one stops working."""
    r, c, keys = farm
    made = (await c.post("/api/admin/setups", json=spec(), headers=H)).json()
    old = "/" + made["script_url"].split("/", 3)[3]
    again = (await c.post(f"/api/admin/setups/{made['id']}/renew", headers=H)).json()
    new = "/" + again["script_url"].split("/", 3)[3]
    assert new != old and again["script_sha256"] in again["command"]
    assert (await c.get(old)).status_code == 404 and (await c.get(new)).status_code == 200
    await c.post(new.replace("/script", "/report"), content=json.dumps({"ok": True, "host_keys": ["ssh-ed25519 AAAAhostkey"]}))
    assert (await c.post(f"/api/admin/setups/{made['id']}/renew", headers=H)).status_code == 409    # reported: add it instead
