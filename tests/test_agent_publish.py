"""Worker-side tools: the restart agent (contract §6) and the artifact publisher (§2)."""
import json
import os
import stat
import subprocess
import sys
import threading
import time

import httpx
import pytest

from comfyfarm import agent, launcher, publish
from conftest import free_port

AGENT = os.path.join(os.path.dirname(agent.__file__), "agent.py")


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    log = tmp_path / "calls.txt"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    s = bin_ / "systemctl"
    s.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                 "case \"$*\" in *show*) printf 'ActiveState=failed\\nExecMainStatus=2\\nNRestarts=3\\n';; esac\n")
    s.chmod(s.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "COMFYFARM_AGENT_INSTANCES": "gpu-1,gpu-2",
           "COMFYFARM_AGENT_USER": "1"}
    return env, log


def run_ssh(env, command):
    return subprocess.run([sys.executable, AGENT, "ssh"], env={**env, "SSH_ORIGINAL_COMMAND": command},
                          capture_output=True, text=True, timeout=30)


def test_agent_ssh_forced_command(fake_systemctl):
    env, log = fake_systemctl
    r = run_ssh(env, "restart gpu-1")
    assert r.returncode == 0
    r = run_ssh(env, "status gpu-2")
    assert r.returncode == 0 and json.loads(r.stdout) == {"ActiveState": "failed", "ExecMainStatus": "2", "NRestarts": "3"}
    assert log.read_text().splitlines() == ["--user restart --no-block comfyfarm@gpu-1",
                                            "--user show comfyfarm@gpu-2 -p ActiveState,ExecMainStatus,NRestarts"]
    # the key can say nothing else
    for bad in ("restart gpu-3", "restart gpu-1; rm -rf /", "stop gpu-1", "restart", "sh -c id", "restart ../gpu-1"):
        r = run_ssh(env, bad)
        assert r.returncode == 2 and "refused" in r.stderr, bad
    assert len(log.read_text().splitlines()) == 2                        # nothing ran


def test_agent_http(fake_systemctl, tmp_path):
    env, log = fake_systemctl
    token = tmp_path / "token"
    token.write_text("t" * 32)
    port = free_port()
    p = subprocess.Popen([sys.executable, AGENT, "serve", "--host", "127.0.0.1", "--port", str(port), "--token-file", str(token)],
                         env=env)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                httpx.get(url)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        auth = {"Authorization": "Bearer " + "t" * 32}
        assert httpx.post(f"{url}/restart", params={"instance": "gpu-1"}).status_code == 401
        assert httpx.post(f"{url}/restart", params={"instance": "gpu-1"}, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert httpx.post(f"{url}/restart", params={"instance": "gpu-9"}, headers=auth).status_code == 403
        assert httpx.post(f"{url}/restart", params={"instance": "gpu-1"}, headers=auth).status_code == 200
        assert httpx.get(f"{url}/status", params={"instance": "gpu-1"}, headers=auth).json()["ExecMainStatus"] == "2"
    finally:
        p.terminate()
        p.wait(5)


def test_publish_tree_and_rules(tmp_path):
    art, src = tmp_path / "art", tmp_path / "release"
    (src / "comfy").mkdir(parents=True)
    (src / "main.py").write_text("print('comfy')\n")
    (src / "comfy" / "x.py").write_text("x = 1\n")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    m = publish.publish_tree(str(art), "core", "comfyui-0.41.0", str(src))
    d = art / "core" / "comfyui-0.41.0"
    assert (d / "READY").exists() and not (d / ".git").exists()
    assert m["tree_sha256"] == launcher.tree_sha256(str(d)) and m["files"] == 2
    with pytest.raises(publish.Refused, match="never reused"):
        publish.publish_tree(str(art), "core", "comfyui-0.41.0", str(src))
    with pytest.raises(publish.Refused, match="no main.py"):
        publish.publish_tree(str(art), "core", "other", str(tmp_path))
    with pytest.raises(publish.Refused, match="bad name"):
        publish.publish_tree(str(art), "nodes", "../x", str(src))


def test_venv_escape_detection(tmp_path):
    v = tmp_path / "venv"
    site = v / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "ok.pth").write_text(f"{site}/sub\nimport x\n# comment\n")
    assert publish.venv_escapes(str(v)) == []
    (site / "evil.pth").write_text("/home/someone/src/pkg\n")
    info = site / "pkg-1.0.dist-info"
    info.mkdir()
    (info / "direct_url.json").write_text(json.dumps({"url": "file:///x", "dir_info": {"editable": True}}))
    bad = publish.venv_escapes(str(v))
    assert any("evil.pth" in b for b in bad) and any("editable" in b for b in bad)


def test_publish_venv(tmp_path):
    """A real (empty) venv, built at its final path, published with READY last."""
    art = tmp_path / "art"
    m = publish.publish_venv(str(art), "plain-1", sys.executable, [], "cpu", [], ["--quiet"])
    d = art / "venv" / "plain-1"
    assert (d / "READY").exists() and (d / "freeze.txt").exists() and m["accelerator"] == "cpu" and m["platform"] == "posix"
    assert os.path.realpath(d / "bin" / "python").startswith("/")
