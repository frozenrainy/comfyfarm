"""Artifact copies (docs/design/16): the signed list, the checked install, the drop box — on files, with the real
ssh-keygen (OpenSSH ≥ 8.1)."""
import io
import json
import os
import subprocess
import tarfile
import threading
import time

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
    # a copy whose service went away mid-way (the host restarted) says "running" for ever: after STALE it is missing
    st = store / "status" / "venv.v-9.json"
    st.write_text(json.dumps({"state": "running", "bytes": 5, "total": 9}))
    assert fetch.status("venv/v-9", str(art), str(store / "requests"), str(store / "status"))["venv/v-9"]["state"] == "running"
    os.utime(st, (time.time() - fetch.STALE - 5,) * 2)
    assert fetch.status("venv/v-9", str(art), str(store / "requests"), str(store / "status")) == {"venv/v-9": {"state": "missing"}}
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


# ---- models (docs/design/17) -----------------------------------------------------------------------------------------
class RangeServer:
    """A router's /api/worker_models/<key>/{signed,file}, honouring Range — or not (`ranges=False`)."""

    def __init__(self, files: dict, ranges=True):
        import http.server
        files_, ranges_ = files, ranges
        self.asked = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                self.asked.append((h.path, h.headers.get("Range")))
                body = files_.get(h.path)
                if body is None:
                    h.send_response(404); h.end_headers(); return
                start = 0
                if ranges_ and (rng := h.headers.get("Range")):
                    start = int(rng.split("=")[1].split("-")[0])
                    h.send_response(206)
                else:
                    h.send_response(200)
                h.send_header("Content-Length", str(len(body) - start)); h.end_headers()
                h.wfile.write(body[start:])

            def log_message(h, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        self.srv.shutdown()


def model_router(tmp_path, key_file, data, folder="checkpoints", filename="tiny.safetensors", ranges=True, serve=None):
    """Signs an entry for `data` and serves it — and `serve` instead of `data` as the file, when given."""
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    entry = fetch.model_entry(folder, filename, len(data), sha)
    p = tmp_path / "entry.json"
    p.write_bytes(entry)
    fetch.sign(str(p), str(key_file))
    key = json.loads(entry)["key"]
    srv = RangeServer({f"/api/worker_models/{key}/signed": json.dumps({"entry": entry.decode(), "sig": (tmp_path / "entry.json.sig").read_text()}).encode(),
                       f"/api/worker_models/{key}/file": data if serve is None else serve}, ranges=ranges)
    return srv, key, sha


def test_a_model_copy_goes_on_where_it_stopped(tmp_path, keys):
    key_file, signers = keys
    data = os.urandom(3 * 1024 * 1024 + 17)
    srv, key, sha = model_router(tmp_path, key_file, data)
    models = tmp_path / "host-models"
    try:
        (models / ".incoming").mkdir(parents=True)
        (models / ".incoming" / f"{key}.part").write_bytes(data[:1024 * 1024])          # an interrupted try
        path = fetch.get_model(srv.url, str(models), key, str(signers), out=io.StringIO())
        assert open(path, "rb").read() == data and path.endswith("/checkpoints/tiny.safetensors")
        assert srv.asked[-1] == (f"/api/worker_models/{key}/file", "bytes=1048576-")      # only the rest was asked for
        assert fetch.model_present(str(models), key) and not os.access(path, os.W_OK) or os.getuid() == 0
        assert not os.path.exists(models / ".incoming" / f"{key}.part")
        n = len(srv.asked)
        fetch.get_model(srv.url, str(models), key, str(signers), out=io.StringIO())        # here: nothing fetched again
        assert len(srv.asked) == n + 1 and srv.asked[-1][0].endswith("/signed")
    finally:
        srv.stop()


def test_a_model_copy_that_does_not_match_is_thrown_away(tmp_path, keys):
    key_file, signers = keys
    data = os.urandom(200_000)
    srv, key, _ = model_router(tmp_path, key_file, data, ranges=False)             # a server without ranges
    try:
        models = tmp_path / "host-models"
        (models / ".incoming").mkdir(parents=True)
        (models / ".incoming" / f"{key}.part").write_bytes(b"x" * 1000)             # junk from before: started over
        fetch.get_model(srv.url, str(models), key, str(signers), out=io.StringIO())
        assert fetch.model_present(str(models), key)
    finally:
        srv.stop()
    srv = model_router(tmp_path, key_file, data, serve=os.urandom(200_000))[0]      # not the file that was signed
    try:
        models = tmp_path / "other-host"
        with pytest.raises(fetch.Refused, match="not the 200000 bytes"):
            fetch.get_model(srv.url, str(models), key, str(signers), out=io.StringIO())
        assert not fetch.model_present(str(models), key) and not os.listdir(models / ".incoming")
        assert not (models / "checkpoints").exists()
    finally:
        srv.stop()


def test_a_model_entry_needs_the_builders_signature_and_a_safe_name(tmp_path, keys):
    key_file, signers = keys
    with pytest.raises(fetch.Refused):
        fetch.model_entry("checkpoints", "../../etc/passwd", 1, "a" * 64)
    with pytest.raises(fetch.Refused):
        fetch.model_entry("custom_nodes", "x.safetensors", 1, "a" * 64)             # not a model folder
    data = os.urandom(1000)
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    srv, key, _ = model_router(tmp_path, other, data)                                # signed by someone else
    try:
        with pytest.raises(fetch.Refused, match="not signed by this farm's builder"):
            fetch.get_model(srv.url, str(tmp_path / "m"), key, str(signers), out=io.StringIO())
    finally:
        srv.stop()
    assert fetch.parse("model/" + key) == [("model", key)]


def test_the_fetch_service_copies_a_model_the_host_asked_for(tmp_path, keys):
    """The drop box takes model/<key> as it takes artifacts (field: the first version dropped them unread)."""
    import hashlib
    key_file, signers = keys
    data = os.urandom(50_000)
    srv, key, _ = model_router(tmp_path, key_file, data)
    store, models = tmp_path / "store", tmp_path / "host-models"
    requests, status_dir = store / "requests", store / "status"
    for x in (requests, status_dir, models):
        x.mkdir(parents=True, exist_ok=True)
    cfg = store / "store.env"
    cfg.write_text(f"COMFYFARM_STORE_ROUTER={srv.url}\nCOMFYFARM_STORE_ART={tmp_path / 'art'}\nCOMFYFARM_STORE_MODELS={models}\n")

    class Child:                                       # `fetch.py get-model` as the store account, here in-process
        def __init__(self, argv, **kw):
            assert argv[-5:-3] == [os.path.join(fetch.HERE, "fetch.py"), "get-model"] and argv[-3:] == [srv.url, str(models), key]
            out = io.StringIO()
            fetch.get_model(srv.url, str(models), key, str(signers), out=out)
            self.stdout, self.stderr = io.StringIO(out.getvalue()), io.StringIO("")

        def wait(self):
            return 0
    try:
        assert fetch.request(f"model/{key}", str(requests)) == f"asked for model/{key}"
        assert fetch.run(str(requests), str(status_dir), str(cfg), popen=Child) == [f"model/{key}"]
        assert fetch.status(f"model/{key}", requests=str(requests), status_dir=str(status_dir), art="",
                            models=str(models)) == {f"model/{key}": {"state": "present"}}
    finally:
        srv.stop()
    # another revision at the same place: the first one's entry goes with its file (same size: it must not count)
    newer = os.urandom(50_000)
    srv, key2, _ = model_router(tmp_path, key_file, newer)
    try:
        fetch.get_model(srv.url, str(models), key2, str(signers), out=io.StringIO())
    finally:
        srv.stop()
    assert fetch.model_present(str(models), key2) and not fetch.model_present(str(models), key)
    assert hashlib.sha256((models / "checkpoints" / "tiny.safetensors").read_bytes()).hexdigest() == hashlib.sha256(newer).hexdigest()
    # a host without a model store: nothing to copy, and it says so instead of "missing" for ever
    assert fetch.status(f"model/{key}", requests=str(requests), status_dir=str(status_dir), art="",
                        models="")[f"model/{key}"]["state"] == "off"
    with pytest.raises(fetch.Refused, match="not an artifact name"):
        fetch.parse("model/../../etc/passwd")
    with pytest.raises(fetch.Refused, match="not an artifact name"):
        fetch.parse("model/tiny.safetensors")                                       # a key, never a file name
