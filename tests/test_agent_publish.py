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
    real = os.path.realpath(d / "bin" / "python")
    assert real.startswith(os.path.realpath(d) + os.sep) or real.startswith("/usr/")       # D-42: runs on any host
    assert publish.interpreter_problems(str(d)) == []
    out = subprocess.run([str(d / "bin" / "python"), "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True)
    assert out.returncode == 0 and os.path.realpath(out.stdout.strip()) == os.path.realpath(d)


def test_a_venv_tied_to_the_build_host_is_refused(tmp_path):
    """D-42: a venv whose interpreter lives in the builder's home (uv's store, a pyenv) only works on that host."""
    v = tmp_path / "v"
    (v / "bin").mkdir(parents=True)
    outside = tmp_path / "home" / ".local" / "share" / "uv" / "python" / "cpython-3.12" / "bin"
    outside.mkdir(parents=True)
    (outside / "python3.12").write_text("")
    os.symlink(outside / "python3.12", v / "bin" / "python")
    (v / "pyvenv.cfg").write_text(f"home = {outside}\nversion_info = 3.12\n")
    bad = publish.interpreter_problems(str(v))
    assert len(bad) == 2 and "bin/python" in bad[0] and "home" in bad[1]


def test_tree_hash_encoding_is_the_contract_one(tmp_path):
    """Contract §2 v1.2: full-line bytes sorted, '\\n' between lines, directory symlinks listed, top-level manifest/READY excluded."""
    import hashlib
    root = tmp_path / "t"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "z.so").write_bytes(b"12345")
    (root / "a").write_bytes(b"xy")
    (root / "lib64").symlink_to("lib")                       # directory symlink: listed, not followed
    (root / "link.txt").symlink_to("a")
    (root / "manifest.json").write_text("{}")
    (root / "READY").write_text("")
    (root / "sub").mkdir()
    (root / "sub" / "manifest.json").write_text("not excluded: not top-level")
    lines = sorted([b"f a 2", b"f lib/z.so 5", b"l lib64 lib", b"l link.txt a", b"f sub/manifest.json 27"])
    assert launcher.tree_sha256(str(root)) == hashlib.sha256(b"\n".join(lines)).hexdigest()
    (tmp_path / "empty").mkdir()
    assert launcher.tree_sha256(str(tmp_path / "empty")) == hashlib.sha256(b"").hexdigest()


def test_publish_refuses_odd_entries(tmp_path):
    import os
    art, src = tmp_path / "art", tmp_path / "n1"
    src.mkdir()
    (src / "ok.py").write_text("x")
    os.mkfifo(src / "pipe")
    with pytest.raises(publish.Refused, match="not a regular file"):
        publish.publish_tree(str(art), "nodes", "n1", str(src))
    src2 = tmp_path / "n2"
    src2.mkdir()
    (src2 / "bad\nname").write_text("x")
    with pytest.raises(publish.Refused, match="newline"):
        publish.publish_tree(str(art), "nodes", "n2", str(src2))


VECTOR_LINES = [b"f a-b/g 3", b"f a/f 2", b"f a/manifest.json 6", b"f a/sub/h 0", b"f lib/m 4", b"f zz.txt 1",
                "f 한글/파일 이름.txt 5".encode(), b"l a/zz-link ../zz.txt", b"l lib64 lib"]
VECTOR_HASH = "d55960db78d265abea7da6772ede6c3fbb8e9090b6359d552861198ead243ccc"


def test_contract_tree_vector(tmp_path):
    """Contract appendix A: the agreed test vector (both implementations produce it). Line ORDER is asserted, not only
    the hash: whole-line order puts every `f` line before every `l` line, unlike a sort by path."""
    t = tmp_path / "t"
    for d in ("a/sub", "a-b", "lib", "empty", "한글"):
        (t / d).mkdir(parents=True)
    for rel, n in (("zz.txt", 1), ("a/f", 2), ("a-b/g", 3), ("a/sub/h", 0), ("lib/m", 4), ("한글/파일 이름.txt", 5),
                   ("a/manifest.json", 6), ("manifest.json", 9), ("READY", 0)):
        (t / rel).write_bytes(b"x" * n)
    (t / "lib64").symlink_to("lib")
    (t / "a" / "zz-link").symlink_to("../zz.txt")
    assert launcher.tree_lines(str(t)) == VECTOR_LINES
    assert launcher.tree_sha256(str(t)) == VECTOR_HASH
    # negative control: a sort by path would put `l lib64` before `f zz.txt`
    by_path = sorted(VECTOR_LINES, key=lambda l: l.split(b" ", 2)[1])
    assert by_path != VECTOR_LINES


def test_tree_refuses_non_utf8_names(tmp_path):
    import os
    root = tmp_path / "u"
    root.mkdir()
    try:
        with open(os.path.join(os.fsencode(str(root)), b"bad\xff"), "wb") as f:
            f.write(b"x")
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses non-UTF-8 names (APFS does); the rule is exercised on ext4/xfs")
    with pytest.raises(ValueError, match="UTF-8"):
        launcher.tree_sha256(str(root))


def test_tree_refuses_newlines_in_link_names_and_targets(tmp_path):
    """QA-11: a newline in a directory-symlink name or in any symlink target would end its hash line early (contract §2:
    newline names are refused; a target with one is refused for the same reason)."""
    import os
    for i, (name, target, is_dir) in enumerate([("bad\nlink", "lib", True), ("ok-link", "a\nb", False), ("ok-dir", "lib\nx", True)]):
        root = tmp_path / f"t{i}"
        (root / "lib").mkdir(parents=True)
        os.symlink(target, root / name, target_is_directory=is_dir)
        with pytest.raises(ValueError, match="newline"):
            launcher.tree_sha256(str(root))
        with pytest.raises(publish.Refused, match="newline"):
            publish.publish_tree(str(tmp_path / "art"), "nodes", f"n{i}", str(root))


def test_publish_symlink_rules(tmp_path):
    """QA-11: no symlinks in a core (contract §2: the release archive as is); node symlinks must stay inside."""
    import os
    art = tmp_path / "art"
    core = tmp_path / "core-src"
    (core / "comfy").mkdir(parents=True)
    (core / "main.py").write_text("x")
    (core / "link.py").symlink_to("main.py")
    with pytest.raises(publish.Refused, match="symlink in a core"):
        publish.publish_tree(str(art), "core", "c1", str(core))
    assert not (art / "core" / "c1").exists() and os.listdir(art / "core") == []     # nothing left, name not used
    for i, target in enumerate(["../../outside", "/etc/passwd", "../.."]):
        nodes = tmp_path / f"nodes{i}"
        (nodes / "sub").mkdir(parents=True)
        (nodes / "sub" / "evil").symlink_to(target)
        with pytest.raises(publish.Refused, match="outside the artifact"):
            publish.publish_tree(str(art), "nodes", f"n{i}", str(nodes))
    ok = tmp_path / "nodes-ok"
    (ok / "pkg" / "lib").mkdir(parents=True)
    (ok / "pkg" / "lib64").symlink_to("lib")
    (ok / "pkg" / "x.py").write_text("x")
    (ok / "pkg" / "y.py").symlink_to("../pkg/x.py")
    publish.publish_tree(str(art), "nodes", "ok", str(ok))                        # positive control


def test_published_artifacts_are_read_only_and_local_copies_prunable(tmp_path):
    """QA-11: READY appears only on a read-only artifact (contract §2); the launcher's local copy is its own again, so
    it can be replaced and pruned."""
    import os
    import stat as st
    art, local = tmp_path / "art", tmp_path / "local"
    names = []
    for i in range(5):
        src = tmp_path / f"src{i}"
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "n.py").write_text(str(i))
        publish.publish_tree(str(art), "nodes", f"n{i}", str(src))
        names.append(f"n{i}")
    d = art / "nodes" / "n0"
    for p in [d, d / "READY", d / "manifest.json", d / "pkg", d / "pkg" / "n.py"]:
        assert not os.lstat(p).st_mode & (st.S_IWUSR | st.S_IWGRP | st.S_IWOTH), p
    for n in names:
        launcher.sync({"src": str(art / "nodes" / n), "dst": str(local / "nodes" / n)})
    assert os.access(local / "nodes" / "n0" / "pkg" / "n.py", os.W_OK)
    launcher.prune(str(local), "nodes", {"n4"})
    assert sorted(os.listdir(local / "nodes")) == ["n2", "n3", "n4"]              # really removed, nothing left over


def test_venv_escape_relative_pth(tmp_path):
    """QA-11: a relative .pth line resolves from the .pth's directory — '..' can leave the venv."""
    v = tmp_path / "venv"
    site = v / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "ok.pth").write_text("sub\n./other\n")
    assert publish.venv_escapes(str(v)) == []
    (site / "up.pth").write_text("../../../../src/pkg\n")
    assert any("up.pth" in b for b in publish.venv_escapes(str(v)))


def test_agent_reads_its_config_file_and_runs_the_host_check(fake_systemctl, tmp_path):
    """A forced command cannot pass environment: setup writes /etc/comfyfarm/agent.env (here: a temp file). `check`
    runs the read-only host check for a served instance only; anything else stays refused."""
    env, log = fake_systemctl
    conf = tmp_path / "agent.env"
    conf.write_text("COMFYFARM_AGENT_INSTANCES=gpu-9\nCOMFYFARM_AGENT_USER=1\n")
    env = {k: v for k, v in env.items() if not k.startswith("COMFYFARM_AGENT_")}
    env["COMFYFARM_AGENT_CONFIG"] = str(conf)
    assert run_ssh(env, "restart gpu-9").returncode == 0 and "--user restart --no-block comfyfarm@gpu-9" in log.read_text()
    assert run_ssh(env, "restart gpu-1").returncode == 2                              # not in the file
    r = run_ssh(env, "check gpu-9")                                                   # no /etc/comfyfarm/gpu-9.* here
    assert r.returncode == 2 and "not set up" in (r.stdout + r.stderr)
    assert run_ssh(env, "check gpu-1").returncode == 2
    assert run_ssh(env, "sh -c id").returncode == 2


def test_publish_venv_takes_a_version_or_name_and_says_when_none_is_found(tmp_path):
    with pytest.raises(publish.Refused, match="no Python 'python9.99' found"):
        publish.publish_venv(str(tmp_path / "art"), "v", "python9.99", [], "cpu", [], [])
