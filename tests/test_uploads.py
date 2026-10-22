"""Uploads (docs/design/18): models and node pack archives from the operator's own files — in chunks that go on where
they stopped, taken in by the builder (never the router), and the inbox on the control-plane host."""
import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile

import httpx
import pytest

from comfyfarm import builder, fetch, publish
from comfyfarm.app import create_app
from conftest import add_worker, fast_settings

H = {"X-ComfyFarm-User": "admin"}


@pytest.fixture
async def farm(dsn, tmp_path):
    builds, models, art, keys = tmp_path / "builds", tmp_path / "models", tmp_path / "art", tmp_path / "keys"
    for d in (builds / "requests", builds / "status", builds / "archives", models / ".uploads", art, keys):
        d.mkdir(parents=True)
    b = builder.Builder(str(builds / "requests"), str(builds / "status"), str(art), str(tmp_path), None,
                        ["https://github.com/"], key=str(tmp_path / "home" / "signing" / "artifacts"), models=str(models),
                        node_archives=True)
    b.ensure_key()
    up = lambda **kw: builder.write_json(str(builds / "status" / "policy.json"), {**b.policy(), **kw})   # noqa: E731
    up()
    app = create_app(fast_settings(dsn, builds_dir=str(builds), models_dir=str(models), artifacts_dir=str(art),
                                   keys_dir=str(keys)))
    async with app.router.lifespan_context(app):
        r = app.state.router
        for t in r.tasks:                       # the test drives the loops' steps itself
            if t.get_coro().__name__ in ("model_loop", "tidy_loop"):
                t.cancel()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield {"r": r, "c": c, "b": b, "builds": builds, "models": models, "up": up, "tmp": tmp_path}


def run_requests(f):
    """The builder's model lane, once: what is waiting."""
    for rid in f["b"].pending("model"):
        f["b"].handle(rid)


async def send(c, uid, data, offset=0, chunk=1000):
    while offset < len(data):
        r = await c.put(f"/api/admin/uploads/{uid}?offset={offset}", content=data[offset:offset + chunk], headers=H)
        assert r.status_code == 200, r.text
        offset = r.json()["received"]
    return r.json()


async def test_a_model_goes_up_in_chunks_and_the_builder_takes_it_in(farm):
    c, r, models = farm["c"], farm["r"], farm["models"]
    follow_builder = r.models_step[0]
    gid, _ = await add_worker(r, "http://192.0.2.21:8188", group="video")
    data = os.urandom(5000)
    start = {"kind": "model", "filename": "my-style.safetensors", "size": len(data), "meta": {"folder": "loras", "groups": [gid]}}
    for bad, code in (({**start, "filename": "x.ckpt"}, 400), ({**start, "meta": {"folder": "custom_nodes"}}, 400),
                      ({**start, "filename": "../x.safetensors"}, 400), ({**start, "size": 0}, 400),
                      ({**start, "kind": "venv"}, 400)):
        assert (await c.post("/api/admin/uploads", json=bad, headers=H)).status_code == code, bad
    assert (await c.post("/api/admin/uploads", json=start, headers={"X-ComfyFarm-User": "mallory"})).status_code == 403
    u = (await c.post("/api/admin/uploads", json=start, headers=H)).json()
    assert u["received"] == 0 and u["chunk"] == 16 << 20
    assert (await c.post("/api/admin/uploads", json=start, headers=H)).status_code == 409        # the same file twice
    await send(c, u["id"], data[:3000])
    # the connection broke; the dashboard asks with an offset the router does not have: it says what it has
    wrong = await c.put(f"/api/admin/uploads/{u['id']}?offset=1000", content=data[1000:2000], headers=H)
    assert wrong.status_code == 409 and wrong.json()["detail"]["received"] == 3000
    too_much = await c.put(f"/api/admin/uploads/{u['id']}?offset=3000", content=data[3000:] + b"x", headers=H)
    assert too_much.status_code == 413
    done = await send(c, u["id"], data, offset=3000)
    assert done["done"] and done["result"]["model_id"]
    listed = (await c.get("/api/admin/models", headers=H)).json()["models"]
    assert [(m["folder"], m["filename"], m["source"], m["state"], m["groups"]) for m in listed] == [
        ("loras", "my-style.safetensors", "upload", "queued", [gid])]
    run_requests(farm)                                                        # the builder copies, hashes, signs
    assert await follow_builder() is True
    m = (await c.get("/api/admin/models", headers=H)).json()["models"][0]
    assert m["state"] == "ready" and m["sha256"] == hashlib.sha256(data).hexdigest()
    dest = models / "loras" / "my-style.safetensors"
    assert dest.read_bytes() == data and (not os.access(dest, os.W_OK) or os.getuid() == 0)
    signers = farm["tmp"] / "pinned"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {farm["b"].public_key()}\n')
    lst = models / "signed" / "model" / f"{m['key']}.json"
    fetch.verify(lst.read_bytes(), (models / "signed" / "model" / f"{m['key']}.json.sig").read_bytes(), str(signers))
    part = models / ".uploads" / f"{u['id']}.part"
    assert part.exists()
    await r.uploads_tidy()                                                    # the builder has it: the upload goes
    assert not part.exists()


async def test_an_unfinished_upload_can_be_cancelled_and_goes_after_a_day(farm):
    c, r, models = farm["c"], farm["r"], farm["models"]
    start = {"kind": "model", "filename": "a.safetensors", "size": 4000, "meta": {"folder": "vae"}}
    a = (await c.post("/api/admin/uploads", json=start, headers=H)).json()
    b = (await c.post("/api/admin/uploads", json={**start, "filename": "b.safetensors"}, headers=H)).json()
    await send(c, a["id"], os.urandom(1000))
    left = [u for u in (await c.get("/api/admin/uploads", headers=H)).json() if u["state"] == "uploading"]
    assert {u["filename"]: u["received"] for u in left} == {"a.safetensors": 1000, "b.safetensors": 0}   # to go on with
    assert (await c.delete(f"/api/admin/uploads/{a['id']}", headers=H)).status_code == 200
    assert not (models / ".uploads" / f"{a['id']}.part").exists()
    gone = await c.put(f"/api/admin/uploads/{a['id']}?offset=1000", content=b"x", headers=H)
    assert gone.status_code == 409 and gone.json()["detail"]["state"] == "cancelled"
    async with r.db.conn() as x:
        await x.execute("UPDATE uploads SET updated_at = now() - interval '25 hours' WHERE id = $1::uuid", b["id"])
    await r.uploads_tidy()
    assert not (models / ".uploads" / f"{b['id']}.part").exists()
    assert [u["state"] for u in (await c.get("/api/admin/uploads", headers=H)).json() if u["id"] == b["id"]] == ["cancelled"]
    # a model store the router cannot write to: said, not a crash
    os.chmod(models / ".uploads", 0o555)
    try:
        if os.getuid() != 0:
            refused = await c.post("/api/admin/uploads", json={**start, "filename": "c.safetensors"}, headers=H)
            assert refused.status_code == 400 and ".uploads" in refused.json()["detail"]
    finally:
        os.chmod(models / ".uploads", 0o755)


def zip_of(files: dict, links=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)
    return buf.getvalue()


async def test_a_node_pack_from_an_archive_is_pinned_built_and_updated(farm):
    c, b, builds = farm["c"], farm["b"], farm["builds"]
    pack = zip_of({"my-pack-main/__init__.py": "NODE_CLASS_MAPPINGS = {}\n", "my-pack-main/requirements.txt": "kornia\n",
                   "my-pack-main/.git/config": "x"})
    sha = hashlib.sha256(pack).hexdigest()
    start = {"kind": "nodepack", "filename": "my-pack-main.zip", "size": len(pack), "meta": {"name": "my-pack", "ui": False}}
    for bad in ({**start, "filename": "x.exe"}, {**start, "meta": {"name": "../x"}}, {**start, "meta": {"name": "comfyfarm_ui"}},
                {**start, "size": 3 << 30}):
        assert (await c.post("/api/admin/uploads", json=bad, headers=H)).status_code == 400, bad
    u = (await c.post("/api/admin/uploads", json=start, headers=H)).json()
    done = await send(c, u["id"], pack, chunk=300)
    assert done["result"]["archive"] == sha and (builds / "archives" / f"{sha}.zip").read_bytes() == pack
    packs = (await c.get("/api/admin/node-packs", headers=H)).json()
    assert [(p["name"], p["archive"], p["archive_name"], p["repo"], p["ui"]) for p in packs] == [
        ("my-pack", sha, "my-pack-main.zip", None, False)]
    # a build carries it by its SHA-256; the builder finds the file itself and checks it when it unpacks
    rid = (await c.post("/api/admin/builds", json={"recipe": "rocm-x", "packs": True}, headers=H))
    assert rid.status_code in (200, 400)                     # no recipe "rocm-x" here: the request body is what counts
    nodes = [{"name": "my-pack", "archive": sha, "archive_name": "my-pack-main.zip", "ui": False}]
    checked = b.check_nodes(nodes)
    assert checked[0]["archive"] == str(builds / "archives" / f"{sha}.zip") and checked[0]["archive_sha256"] == sha
    tree = publish.unpack(checked[0]["archive"], sha, str(farm["tmp"] / "unpacked"))
    assert sorted(os.listdir(tree)) == ["__init__.py", "requirements.txt"]      # the top directory stripped, .git left out
    role = {"accelerator": "cpu", "index": "https://download.pytorch.org/whl/cpu", "venv": "v"}
    recipe = {"recipe": "x", "comfyui": {"repo": "https://github.com/a/b", "ref": "v1"}, "gpu": role, "ui": role}
    assert publish.check_recipe({**recipe, "nodes": [checked[0]]})            # an archive node, as the builder passes it
    with pytest.raises(publish.Refused, match="archive"):
        publish.check_recipe({**recipe, "nodes": [{**checked[0], "repo": "https://github.com/a/b"}]})   # not both
    with pytest.raises(builder.Refused, match="is not in"):
        b.check_nodes([{**nodes[0], "archive": "f" * 64}])
    b.node_archives = False
    with pytest.raises(builder.Refused, match="COMFYFARM_BUILD_NODE_ARCHIVES"):
        b.check_nodes(nodes)
    # a new version: the same pack, another archive
    newer = zip_of({"my-pack/__init__.py": "NODE_CLASS_MAPPINGS = {'X': None}\n"})
    u2 = (await c.post("/api/admin/uploads", json={**start, "filename": "my-pack.zip", "size": len(newer),
                                                   "meta": {"pack_id": packs[0]["id"]}}, headers=H)).json()
    await send(c, u2["id"], newer)
    p = (await c.get("/api/admin/node-packs", headers=H)).json()[0]
    assert p["archive"] == hashlib.sha256(newer).hexdigest() and p["name"] == "my-pack" and p["ui"] is False
    # an archive pack is not edited into a git one
    assert (await c.patch(f"/api/admin/node-packs/{p['id']}", json={"commit": "a" * 40}, headers=H)).status_code == 400
    assert (await c.patch(f"/api/admin/node-packs/{p['id']}", json={"note": "ours"}, headers=H)).status_code == 200
    farm["up"](node_archives=False)                          # the builder's switch off: the router refuses uploads
    off = await c.post("/api/admin/uploads", json={**start, "meta": {"name": "other"}}, headers=H)
    assert off.status_code == 400 and "COMFYFARM_BUILD_NODE_ARCHIVES" in off.json()["detail"]


def tar_of(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            elif kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, data
                t.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, data
                t.addfile(info)
            elif kind == "dev":
                info.type = tarfile.CHRTYPE
                t.addfile(info)
    return buf.getvalue()


@pytest.mark.parametrize("case", ["dotdot", "absolute", "zip link", "tar link", "hard link", "device", "too big",
                                  "not the pinned one", "not an archive"])
def test_an_archive_that_reaches_outside_itself_is_refused(tmp_path, case, monkeypatch):
    data = {"dotdot": zip_of({"pack/../../evil.py": "x"}), "absolute": zip_of({"/etc/evil": "x"}),
            "zip link": zip_of({"pack/a.py": "x"}, links=[("pack/b", "/etc/passwd")]),
            "tar link": tar_of([("pack/a.py", "file", b"x"), ("pack/b", "sym", "/etc/passwd")]),
            "hard link": tar_of([("pack/a.py", "file", b"x"), ("pack/b", "hard", "pack/a.py")]),
            "device": tar_of([("pack/a.py", "file", b"x"), ("pack/null", "dev", None)]),
            "too big": zip_of({"pack/a.bin": "x" * 5000}), "not the pinned one": zip_of({"pack/a.py": "x"}),
            "not an archive": b"just text, nothing packed"}[case]
    path = tmp_path / "a"
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest() if case != "not the pinned one" else "0" * 64
    monkeypatch.setattr(publish, "UNPACKED_MAX", 1000)
    want = {"dotdot": "outside itself", "absolute": "outside itself", "zip link": "link", "tar link": "link",
            "hard link": "link", "device": "special", "too big": "more than", "not the pinned one": "not the archive",
            "not an archive": "not a zip or tar"}[case]
    with pytest.raises(publish.Refused, match=want):
        publish.unpack(str(path), sha, str(tmp_path / "out"))
    assert not (tmp_path / "evil.py").exists() and not (tmp_path.parent / "evil.py").exists()


def test_an_archive_without_a_single_top_directory_is_the_pack_itself(tmp_path):
    data = tar_of([("__init__.py", "file", b"x"), ("nodes/a.py", "file", b"y")])
    (tmp_path / "a.tgz").write_bytes(data)
    out = publish.unpack(str(tmp_path / "a.tgz"), hashlib.sha256(data).hexdigest(), str(tmp_path / "out"))
    assert sorted(os.listdir(out)) == ["__init__.py", "nodes"]


async def test_the_inbox_takes_in_what_an_operator_copied(farm):
    c, r, b, models = farm["c"], farm["r"], farm["b"], farm["models"]
    for folder in fetch.MODEL_FOLDERS:
        (models / "inbox" / folder).mkdir(parents=True)
    good, bad_mode, pickle = os.urandom(3000), os.urandom(10), os.urandom(10)
    (models / "inbox" / "loras" / "detail.safetensors").write_bytes(good)
    os.chmod(models / "inbox" / "loras" / "detail.safetensors", 0o644)       # as an operator leaves it (any umask here)
    (models / "inbox" / "loras" / "private.safetensors").write_bytes(bad_mode)
    os.chmod(models / "inbox" / "loras" / "private.safetensors", 0o600)
    (models / "inbox" / "checkpoints" / "old.ckpt").write_bytes(pickle)
    (models / "inbox" / "vae" / "bad name!.safetensors").write_bytes(b"x")
    for p in ("checkpoints/old.ckpt", "vae/bad name!.safetensors"):
        os.chmod(models / "inbox" / p, 0o644)
    os.symlink("/etc/passwd", models / "inbox" / "vae" / "link.safetensors")
    (models / "vae").mkdir()
    (models / "vae" / "taken.safetensors").write_bytes(b"already")
    (models / "inbox" / "vae" / "taken.safetensors").write_bytes(b"another")
    os.chmod(models / "inbox" / "vae" / "taken.safetensors", 0o644)
    rid = (await c.post("/api/admin/models/inbox", headers=H)).json()["id"]
    assert (await c.post("/api/admin/models/inbox", headers=H)).status_code == 409          # one scan at a time
    run_requests(farm)
    got = (await c.get("/api/admin/models", headers=H)).json()
    results = {f"{x['folder']}/{x['filename']}": x.get("error") or "ok" for x in got["inbox"]["results"]}
    assert results["loras/detail.safetensors"] == "ok"
    assert "readable by everyone" in results["loras/private.safetensors"] or os.getuid() == 0
    assert "only .safetensors" in results["checkpoints/old.ckpt"]
    assert "file name" in results["vae/bad name!.safetensors"]
    assert "not a plain file" in results["vae/link.safetensors"]
    assert "in the store already" in results["vae/taken.safetensors"]
    assert (models / "loras" / "detail.safetensors").read_bytes() == good                  # moved, not copied
    assert not (models / "inbox" / "loras" / "detail.safetensors").exists()
    assert (models / "inbox" / "checkpoints" / "old.ckpt").exists()                        # what failed stays
    # the router lists what the builder took in (ready, no groups), once
    assert await r.models_register_inbox() is True and await r.models_register_inbox() is False
    listed = {(m["folder"], m["filename"]): m for m in (await c.get("/api/admin/models", headers=H)).json()["models"]}
    m = listed[("loras", "detail.safetensors")]
    assert (m["source"], m["state"], m["sha256"], m["groups"]) == ("inbox", "ready", hashlib.sha256(good).hexdigest(), [])
    assert fetch.model_present(str(models), m["key"])


def test_a_request_no_lane_takes_is_failed_not_left_waiting(tmp_path):
    for d in ("requests", "status", "art"):
        (tmp_path / d).mkdir()
    b = builder.Builder(str(tmp_path / "requests"), str(tmp_path / "status"), str(tmp_path / "art"), str(tmp_path), None, [])
    for kind, rid in (("model-file", "1792000000000-00000001"), ("mystery", "1792000000000-00000002"),
                      ({"x": 1}, "1792000000000-00000003")):
        (tmp_path / "requests" / f"{rid}.json").write_text(json.dumps({"kind": kind}))
    assert b.pending("model") == ["1792000000000-00000001"]                   # the model lane takes uploads
    assert b.pending("build") == []
    failed = {p.name: json.loads(p.read_text())["error"] for p in (tmp_path / "status").glob("*.json")}
    assert failed == {"1792000000000-00000002.json": "unknown request kind 'mystery'",
                      "1792000000000-00000003.json": "unknown request kind 'None'"}
