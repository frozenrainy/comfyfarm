"""Artifact copies (docs/design/16): the signed list, the checked install, the drop box — on files, with the real
ssh-keygen (OpenSSH ≥ 8.1)."""
import io
import json
import os
import subprocess
import tarfile
import threading

import pytest

from comfyfarm import fetch


def published(root, kind="venv", name="v-1"):
    """A small artifact as publish.py leaves it: files, a subdirectory, a link, sealed read-only, READY."""
    d = root / kind / name
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python").write_text("#!/bin/sh\n")
    os.chmod(d / "bin" / "python", 0o755)
    (d / "lib" / "pkg").mkdir(parents=True)
    (d / "lib" / "pkg" / "__init__.py").write_text("x = 1\n" * 1000)
    os.symlink("python", d / "bin" / "python3")
    (d / "manifest.json").write_text('{"kind": "venv"}')
    for dirpath, dirs, files in os.walk(d, topdown=False):
        for n in dirs + files:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                os.chmod(p, os.lstat(p).st_mode & ~0o222)
    (d / "READY").write_text("")
    os.chmod(d, 0o555)
    return d


@pytest.fixture
def keys(tmp_path):
    key = tmp_path / "keys" / "artifacts"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "comfyfarm-builder", "-f", str(key)], check=True)
    signers = tmp_path / "keys" / "allowed"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {(key.parent / "artifacts.pub").read_text()}')
    return key, signers


def signed(src_art, kind, name, key):
    listing = fetch.file_list(str(src_art / kind / name), kind, name)
    path = src_art / f"{kind}-{name}.json"
    path.write_bytes(listing)
    fetch.sign(str(path), str(key))
    return listing, (src_art / f"{kind}-{name}.json.sig").read_bytes()


def tar_of(d, extra=None, drop=None):
    """What the router streams: the artifact's tree (READY left out), optionally changed by a man in the middle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for dirpath, dirs, files in os.walk(d):
            dirs.sort()
            for n in sorted(dirs + files):
                p = os.path.join(dirpath, n)
                rel = "./" + os.path.relpath(p, d)
                if rel == "./READY" or rel == drop:
                    continue
                tar.add(p, arcname=rel, recursive=False)
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_the_list_is_the_same_on_both_sides_and_says_everything(tmp_path):
    d = published(tmp_path / "art")
    a = json.loads(fetch.file_list(str(d), "venv", "v-1"))
    assert fetch.file_list(str(d), "venv", "v-1") == fetch.file_list(str(d), "venv", "v-1")      # deterministic
    kinds = {e[1]: e[0] for e in a["entries"]}
    assert kinds == {"bin": "d", "bin/python": "f", "bin/python3": "l", "lib": "d", "lib/pkg": "d",
                     "lib/pkg/__init__.py": "f", "manifest.json": "f"}                     # READY is not in it
    py = next(e for e in a["entries"] if e[1] == "bin/python")
    assert py[2] == 0o555 and py[3] == 10 and len(py[4]) == 64
    assert next(e for e in a["entries"] if e[1] == "bin/python3")[2] == "python"


def test_a_signed_copy_arrives_whole_and_read_only(tmp_path, keys):
    key, signers = keys
    src = tmp_path / "control"
    d = published(src)
    listing, sig = signed(src, "venv", "v-1", key)
    fetch.verify(listing, sig, str(signers))
    art = tmp_path / "host"
    seen = []
    path = fetch.install(tar_of(d), str(art), "venv", "v-1", listing, sig, progress=seen.append)
    assert path == str(art / "venv" / "v-1") and os.path.isfile(art / "venv" / "v-1" / "READY")
    assert fetch.file_list(path, "venv", "v-1") == listing                                  # the same tree
    assert os.readlink(art / "venv" / "v-1" / "bin" / "python3") == "python"
    assert not os.access(art / "venv" / "v-1" / "lib" / "pkg" / "__init__.py", os.W_OK) or os.getuid() == 0
    assert (art / "signed" / "venv" / "v-1.json").read_bytes() == listing and seen
    assert not [n for n in os.listdir(art / "venv") if n.startswith(".incoming")]
    # once there, nothing is fetched again
    assert fetch.install(io.BytesIO(b""), str(art), "venv", "v-1", listing, sig) == path


@pytest.mark.parametrize("how", ["changed byte", "extra file", "missing file", "escape", "other link"])
def test_anything_but_the_signed_tree_installs_nothing(tmp_path, keys, how):
    key, _ = keys
    src = tmp_path / "control"
    d = published(src)
    listing, sig = signed(src, "venv", "v-1", key)
    if how == "changed byte":
        os.chmod(d / "lib" / "pkg", 0o755); os.chmod(d / "lib" / "pkg" / "__init__.py", 0o644)
        (d / "lib" / "pkg" / "__init__.py").write_text("x = 2\n" * 1000)               # same size, other bytes
        stream = tar_of(d)
    elif how == "extra file":
        stream = tar_of(d, extra={"./lib/pkg/evil.py": b"import os"})
    elif how == "missing file":
        stream = tar_of(d, drop="./manifest.json")
    elif how == "escape":
        stream = tar_of(d, extra={"../../outside": b"x"})
    else:
        os.chmod(d / "bin", 0o755); os.remove(d / "bin" / "python3"); os.symlink("/etc/passwd", d / "bin" / "python3")
        stream = tar_of(d)
    art = tmp_path / "host"
    with pytest.raises(fetch.Refused):
        fetch.install(stream, str(art), "venv", "v-1", listing, sig)
    assert not (art / "venv" / "v-1").exists() and not os.path.exists(tmp_path / "outside")
    assert not [n for n in os.listdir(art / "venv") if n.startswith(".incoming")]


def test_only_the_pinned_builder_key_is_accepted(tmp_path, keys):
    key, signers = keys
    src = tmp_path / "control"
    published(src)
    listing, sig = signed(src, "venv", "v-1", key)
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    path = src / "forged.json"
    path.write_bytes(listing)
    fetch.sign(str(path), str(other))
    with pytest.raises(fetch.Refused, match="not signed by this farm's builder"):
        fetch.verify(listing, (src / "forged.json.sig").read_bytes(), str(signers))
    with pytest.raises(fetch.Refused, match="not signed"):                                    # a changed list
        fetch.verify(listing.replace(b"v-1", b"v-2"), sig, str(signers))
    with pytest.raises(fetch.Refused, match="missing"):
        fetch.verify(listing, sig, str(tmp_path / "nowhere"))


def test_requests_status_and_the_root_half(tmp_path, keys):
    key, signers = keys
    src = tmp_path / "control"
    d = published(src)
    listing, sig = signed(src, "venv", "v-1", key)
    store = tmp_path / "store"
    requests, status_dir, art = store / "requests", store / "status", tmp_path / "host"
    for x in (requests, status_dir, art):
        x.mkdir(parents=True)
    cfg = store / "store.env"
    cfg.write_text(f"COMFYFARM_STORE_ROUTER=http://router\nCOMFYFARM_STORE_ART={art}\n")
    names = "venv/v-1"
    assert fetch.request(names, str(requests)) == "asked for venv/v-1"
    assert fetch.request(names, str(requests)) == "already asked for"
    assert fetch.status(names, str(art), str(requests), str(status_dir)) == {"venv/v-1": {"state": "queued"}}
    (requests / "venv.x;rm -rf").write_text("")                                   # junk in the drop box: taken, not run
    os.symlink("/etc", requests / "core.link")

    class Child:                                       # `fetch.py get` as the store account, here in-process
        def __init__(self, argv, **kw):
            assert argv[:4] == ["runuser", "-u", "comfyfarm-store", "--"] and argv[-4:] == ["http://router", str(art), "venv", "v-1"]
            out = io.StringIO()
            fetch.install(tar_of(d), str(art), "venv", "v-1", listing, sig, progress=lambda n: print(
                json.dumps({"bytes": n, "total": 6010}), file=out))
            self.stdout, self.stderr = io.StringIO(out.getvalue()), io.StringIO("")

        def wait(self):
            return 0
    assert fetch.run(str(requests), str(status_dir), str(cfg), popen=Child) == ["venv/v-1"]
    assert os.listdir(requests) == []
    assert fetch.status(names, str(art), str(requests), str(status_dir)) == {"venv/v-1": {"state": "present"}}
    assert json.loads((status_dir / "venv.v-1.json").read_text())["state"] == "done"

    class Broken(Child):
        def __init__(self, argv, **kw):
            self.stdout, self.stderr = io.StringIO(""), io.StringIO("fetch: the list of files is not signed by this farm's builder\n")

        def wait(self):
            return 2
    fetch.request("core/c-1", str(requests))
    fetch.run(str(requests), str(status_dir), str(cfg), popen=Broken)
    st = fetch.status("core/c-1", str(art), str(requests), str(status_dir))["core/c-1"]
    assert st["state"] == "failed" and "not signed" in st["error"]

    def service():                                   # the launcher asks again: the service tries again, fails again
        import time
        time.sleep(0.3)
        fetch.run(str(requests), str(status_dir), str(cfg), popen=Broken)
    threading.Thread(target=service).start()
    with pytest.raises(fetch.Refused, match="copying core/c-1 failed: fetch: the list of files is not signed"):
        fetch.wait("core/c-1", str(art), 5, requests=str(requests), status_dir=str(status_dir), poll=0.05)


def test_the_launcher_waits_for_a_copy_then_goes_on(tmp_path):
    store, art = tmp_path / "store", tmp_path / "host"
    (store / "requests").mkdir(parents=True); (store / "status").mkdir(); art.mkdir()
    said = []

    def arrive():                                   # the fetch service, a moment later
        import time
        time.sleep(0.3)
        os.remove(store / "requests" / "core.c-1")
        (art / "core" / "c-1").mkdir(parents=True)
        (art / "core" / "c-1" / "READY").write_text("")
    threading.Thread(target=arrive).start()
    fetch.wait("core/c-1", str(art), 10, say=said.append, requests=str(store / "requests"),
               status_dir=str(store / "status"), poll=0.05)
    assert os.path.isfile(art / "core" / "c-1" / "READY")
    with pytest.raises(fetch.Refused, match="not set up on this host"):
        fetch.request("core/c-1", str(tmp_path / "nowhere"))
    with pytest.raises(fetch.Refused, match="not an artifact name"):
        fetch.parse("venv/../../etc")


def test_the_agent_takes_artifact_names_only(tmp_path, monkeypatch):
    """The router's key reaches `fetch` / `fetch-status` through the forced command: names, never paths or URLs."""
    from comfyfarm import agent
    store, art = tmp_path / "store", tmp_path / "art"
    (store / "requests").mkdir(parents=True); (store / "status").mkdir(); art.mkdir()
    monkeypatch.setattr(fetch, "REQUESTS", str(store / "requests"))
    monkeypatch.setattr(fetch, "STATUS", str(store / "status"))
    cfg = tmp_path / "store.env"
    cfg.write_text(f"COMFYFARM_STORE_ART={art}\n")
    monkeypatch.setattr(fetch, "CONFIG", str(cfg))
    assert agent.do("fetch", "venv/v-1,core/c-1") == (0, "asked for venv/v-1, core/c-1")
    rc, out = agent.do("fetch-status", "venv/v-1")
    assert rc == 0 and json.loads(out) == {"venv/v-1": {"state": "queued"}}
    for bad in ("venv/../../etc", "/etc/passwd", "http://evil/x", "venv/v-1;id"):
        with pytest.raises(agent.Refused):
            agent.do("fetch", bad)
    monkeypatch.setattr(fetch, "REQUESTS", str(tmp_path / "nowhere"))         # a host with a mounted store
    with pytest.raises(agent.Refused, match="not set up on this host"):
        agent.do("fetch", "venv/v-1")


async def test_the_ssh_backend_reads_an_agent_without_copies_as_nothing_to_do(monkeypatch, tmp_path):
    from comfyfarm.rollout import backends as be
    (tmp_path / "k").write_text("key")
    b = be.make("ssh", {"host": "192.0.2.21", "user": "comfyfarm", "key": "k"}, keys_dir=str(tmp_path))
    answers = iter([(2, "refused: unknown command 'fetch-status'"),
                    (2, "refused: copies are not set up on this host (its artifact store is mounted, …)"),
                    (0, '{"venv/v-1": {"state": "present"}}'),
                    (2, "refused: something else")])

    async def run(argv, timeout=30.0):
        return next(answers)
    monkeypatch.setattr(be, "_run", run)
    assert await b.fetch(["venv/v-1"], state=True) is None                     # older worker tools
    assert await b.fetch(["venv/v-1"], state=True) is None                     # a mounted store
    assert await b.fetch(["venv/v-1"], state=True) == {"venv/v-1": {"state": "present"}}
    with pytest.raises(be.BackendError, match="something else"):
        await b.fetch(["venv/v-1"], state=True)
    with pytest.raises(be.BackendError, match="bad artifact names"):
        await b.fetch(["../x"])
