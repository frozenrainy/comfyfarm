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


async def test_the_inbox_puts_what_it_takes_in_into_the_groups_asked_for(farm):
    c, r, models = farm["c"], farm["r"], farm["models"]
    g1, _ = await add_worker(r, "http://192.0.2.21:8188", group="video")
    for i in range(3):
        p = models / "inbox" / "loras" / f"l{i}.safetensors"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(100))
        os.chmod(p, 0o644)
    assert (await c.post("/api/admin/models/inbox", json={"groups": [99999]}, headers=H)).status_code == 400
    await c.post("/api/admin/models/inbox", json={"groups": [g1]}, headers=H)
    run_requests(farm)
    assert await r.models_register_inbox() is True
    listed = (await c.get("/api/admin/models", headers=H)).json()
    assert sorted((m["filename"], m["groups"]) for m in listed["models"]) == [(f"l{i}.safetensors", [g1]) for i in range(3)]
    assert listed["inbox"]["groups"] == [g1]


async def test_a_model_deleted_from_the_farm_leaves_the_store_and_the_workers(farm, monkeypatch):
    """docs/design/17 phase 2: out of its groups, out of the store (the builder), and the hosts drop their copies; the
    same place can be filled again. Taken out of a group, that group's workers drop theirs too."""
    from comfyfarm.rollout import backends as be
    from test_models import FakeHost
    c, r, b, models = farm["c"], farm["r"], farm["b"], farm["models"]
    follow_builder, follow_copies, copies = r.models_step
    gid, wid = await add_worker(r, "http://192.0.2.21:8188", group="video", name="gpu-a")
    data = os.urandom(4000)
    part = farm["tmp"] / "m.part"
    part.write_bytes(data)
    key = b.place_model(str(part), "loras", "gone.safetensors", len(data), hashlib.sha256(data).hexdigest())
    async with r.db.conn() as x:
        await x.execute("INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, restart_backend, "
                        "restart_config, model_folders) VALUES ($1, 'gpu-a', 'gpu', 'posix', $2, '0.0.0.0', 8188, $3, 'ssh', "
                        "'{}'::jsonb, $4)", wid, ["192.0.2.21"], {"model_store": "/srv/ms"}, list(fetch.MODEL_FOLDERS))
        mid = await x.fetchval("INSERT INTO models(folder, filename, source, state, key, size) VALUES ('loras', "
                               "'gone.safetensors', 'upload', 'ready', $1, $2) RETURNING id", key, len(data))
        await x.execute("INSERT INTO group_models VALUES ($1, $2)", gid, mid)
    host = FakeHost({f"model/{key}": {"state": "present"}})
    monkeypatch.setattr(be, "make", lambda *a, **k: host)
    refreshed = []

    async def refresh(gid, prefer=None):                      # the group's node definitions: read again (not here)
        refreshed.append(gid)
    monkeypatch.setattr(r, "refresh_snapshot", refresh)
    await follow_copies()
    assert host.forgot == [] and copies[wid]["present"] == 1                    # wanted: kept
    assert (await c.delete(f"/api/admin/models/{mid}", headers=H)).json() == {"deleting": True}
    listed = (await c.get("/api/admin/models", headers=H)).json()["models"]
    assert [(m["state"], m["groups"]) for m in listed] == [("deleting", [])]
    assert (await c.patch(f"/api/admin/models/{mid}", json={"groups": [gid]}, headers=H)).status_code == 409
    run_requests(farm)                                                         # the builder takes it out of the store
    assert not (models / "loras" / "gone.safetensors").exists() and not fetch.model_present(str(models), key)
    assert await follow_builder() is True
    assert (await c.get("/api/admin/models", headers=H)).json()["models"] == []
    await follow_copies()
    assert host.forgot == [[f"model/{key}"]]                                    # the host drops its copy
    await follow_copies()
    assert refreshed == [gid]                                                   # its model lists read again
    assert len(host.forgot) == 1                                                # gone: not asked again
    async with r.db.conn() as x:                                               # the same place can be filled again
        again = await x.fetchval("INSERT INTO models(folder, filename, source, state) VALUES ('loras', 'gone.safetensors', "
                                 "'upload', 'queued') RETURNING id")
    assert again != mid
    # taken out of a group: that group's workers drop their copies; a host with older tools says so
    async with r.db.conn() as x:
        m2 = await x.fetchval("INSERT INTO models(folder, filename, source, state, key) VALUES ('vae', 'v.safetensors', "
                              "'inbox', 'ready', $1) RETURNING id", "e" * 32)
        await x.execute("INSERT INTO group_models VALUES ($1, $2)", gid, m2)
    host.states[f"model/{'e' * 32}"] = {"state": "present"}
    await follow_copies()
    assert len(host.forgot) == 1
    assert (await c.post("/api/admin/models/groups", json={"models": [m2], "remove": [gid]}, headers=H)).status_code == 200
    host.old = True
    await follow_copies()
    assert host.forgot[-1] == [f"model/{'e' * 32}"] and "set the worker up again" in copies[wid]["error"]
    # a failed one still goes from the list at once; one still downloading cannot be deleted
    async with r.db.conn() as x:
        f = await x.fetchval("INSERT INTO models(folder, filename, state) VALUES ('vae', 'f.safetensors', 'failed') RETURNING id")
        q = await x.fetchval("INSERT INTO models(folder, filename, state) VALUES ('vae', 'q.safetensors', 'downloading') RETURNING id")
    assert (await c.delete(f"/api/admin/models/{f}", headers=H)).json() == {"deleted": True}
    assert (await c.delete(f"/api/admin/models/{q}", headers=H)).status_code == 409


async def test_a_farm_adds_model_folders_of_its_own(farm):
    """Field (dev.18): frame_interpolation and geometry_estimation were missing, and custom nodes have their own.
    Hosts set up since 0.1.0-dev.19 list the farm's folders; older ones keep the legacy list (their launcher)."""
    from comfyfarm.rollout import render as rd
    c, r = farm["c"], farm["r"]
    _, new = await add_worker(r, "http://192.0.2.21:8188", group="video", name="gpu-new")
    _, old = await add_worker(r, "http://192.0.2.22:8188", group="video", name="gpu-old")
    for wid, open_ in ((new, True), (old, None)):
        put = await c.put(f"/api/admin/instances/{wid}", headers=H, json={
            "instance": f"i{wid}", "hosts": ["127.0.0.1"], "port": 8188, "restart_backend": "manual", "open_folders": open_,
            "paths": {"art": "/a", "local": "/l", "models": "/m", "input": "/s/i", "output": "/s/o", "user": "/s/u",
                      "temp": "/t", "model_store": "/srv/ms"}})
        assert put.status_code == 200, put.text
    folders = (await c.get("/api/admin/models", headers=H)).json()["builder"]["folders"]
    assert "frame_interpolation" in folders and "geometry_estimation" in folders
    for bad, code in (("../x", 400), ("custom_nodes", 400), ("Has Caps", 400), ("loras", 409), ("inbox", 400)):
        assert (await c.post("/api/admin/models/folders", json={"name": bad}, headers=H)).status_code == code, bad
    assert (await c.post("/api/admin/models/folders", json={"name": "ipadapter"}, headers=H)).status_code == 200
    got = (await c.get("/api/admin/models", headers=H)).json()["builder"]
    assert got["custom_folders"] == ["ipadapter"] and got["folders"][-1] == "ipadapter"
    async with r.db.conn() as x:
        rows = {w: dict(await x.fetchrow("SELECT * FROM instances WHERE worker_id = $1", w)) for w in (new, old)}
    assert rows[new]["model_folders"][-1] == "ipadapter" and rows[old]["model_folders"] is None
    profile = {"name": "p", "core": "c", "venv": "v", "nodes": [], "flags": [], "env": {}}
    new_yaml = next(iter(rd.build(profile, rows[new])["files"].values()))
    old_yaml = next(iter(rd.build(profile, rows[old])["files"].values()))
    assert "  ipadapter: ipadapter\n" in new_yaml and "  frame_interpolation: frame_interpolation\n" in new_yaml
    assert "ipadapter" not in old_yaml and "frame_interpolation" not in old_yaml          # what its launcher takes
    up = (await c.post("/api/admin/uploads", json={"kind": "model", "filename": "ip.safetensors", "size": 10,
                                                   "meta": {"folder": "ipadapter"}}, headers=H))
    assert up.status_code == 200
    assert (await c.delete("/api/admin/models/folders/loras", headers=H)).status_code == 409   # ComfyUI's own
    async with r.db.conn() as x:
        await x.execute("INSERT INTO models(folder, filename, state) VALUES ('ipadapter', 'a.safetensors', 'failed')")
    assert (await c.delete("/api/admin/models/folders/ipadapter", headers=H)).status_code == 409   # in use
    async with r.db.conn() as x:
        await x.execute("DELETE FROM models WHERE folder = 'ipadapter'")
    assert (await c.delete("/api/admin/models/folders/ipadapter", headers=H)).status_code == 200
    async with r.db.conn() as x:
        assert "ipadapter" not in await x.fetchval("SELECT model_folders FROM instances WHERE worker_id = $1", new)


async def test_a_worker_running_the_farms_folders_is_shown_as_matching(farm):
    """Field (dev.19): a worker set up again and restarted ran the render with the farm's folders, and the dashboard
    said `differs` — the instance list built the render without the instance's model folders."""
    from comfyfarm.rollout import render as rd
    c, r = farm["c"], farm["r"]
    _, wid = await add_worker(r, "http://192.0.2.21:8188", group="video", name="gpu-1")
    put = await c.put(f"/api/admin/instances/{wid}", headers=H, json={
        "instance": "gpu-1", "hosts": ["127.0.0.1"], "port": 8188, "restart_backend": "manual", "open_folders": True,
        "paths": {"art": "/a", "local": "/l", "models": "/m", "input": "/s/i", "output": "/s/o", "user": "/s/u",
                  "temp": "/t", "model_store": "/srv/ms"}})
    assert put.status_code == 200, put.text
    async with r.db.conn() as x:
        pid = await x.fetchval("INSERT INTO profiles(name, core, venv) VALUES ('p', 'c', 'v') RETURNING id")
        await x.execute("UPDATE instances SET profile_id = $2 WHERE worker_id = $1", wid, pid)
        inst = dict(await x.fetchrow("SELECT * FROM instances WHERE worker_id = $1", wid))
        prof = dict(await x.fetchrow("SELECT * FROM profiles WHERE id = $1", pid))
        running = rd.build(prof, inst)                                   # what the launcher started it with
        await x.execute("UPDATE workers SET capabilities = $2 WHERE id = $1", wid, {"argv": running["argv"][1:]})
    got = next(i for i in (await c.get("/api/admin/instances", headers=H)).json() if i["worker_id"] == wid)
    assert (got["badge"], got["rev"]) == ("match", running["rev"])


def test_two_versions_of_one_pack_in_a_node_set_are_refused_before_the_build(tmp_path):
    """Field (10-28): the new stage_carry zip was added as a second pack next to the old one; both unpack from
    stage_carry/, register the same route, and ComfyUI died at start on every worker of the set."""
    def pack(name, files):
        (tmp_path / name).write_bytes(zip_of(files))
        return {"name": name.removesuffix(".zip"), "archive": str(tmp_path / name)}
    old = pack("stage_carry-bcb08ff.zip", {"stage_carry/__init__.py": "a", "stage_carry/web/x.js": "b"})
    new = pack("stage_carry-fb0af5f.zip", {"stage_carry/__init__.py": "a2"})
    other = pack("story_prompt_gen.zip", {"story_prompt_gen/__init__.py": "c"})
    loose = pack("loose.zip", {"__init__.py": "d"})                         # no top folder: nothing to compare by
    loose2 = pack("loose2.zip", {"__init__.py": "e"})
    publish.refuse_same_pack([old, other, loose, loose2])                 # different packs: fine
    with pytest.raises(publish.Refused, match=r"stage_carry-bcb08ff and stage_carry-fb0af5f are the same pack "
                                              r"\(archive folder stage_carry/\).*update stage_carry-bcb08ff"):
        publish.refuse_same_pack([old, other, new])
    a = {"name": "kj", "repo": "https://github.com/kijai/ComfyUI-KJNodes.git"}
    b = {"name": "kj2", "repo": "https://github.com/kijai/comfyui-kjnodes/"}
    with pytest.raises(publish.Refused, match="kj and kj2 are the same pack"):
        publish.refuse_same_pack([a, b])
    publish.refuse_same_pack([a, {"name": "vhs", "repo": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite"}])
