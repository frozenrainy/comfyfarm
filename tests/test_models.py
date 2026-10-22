"""Models from the dashboard, the router's side (docs/design/17): the list and its checks, the builder's requests and
what comes back, the store's files for the farm's hosts only, and copies asked of each host's agent in the background."""
import hashlib
import json
import os
import subprocess

import httpx
import pytest

from comfyfarm import builder, fetch
from comfyfarm.app import create_app
from comfyfarm.rollout import backends as be
from conftest import add_worker, fast_settings

H = {"X-ComfyFarm-User": "admin"}
SAFE = [".safetensors", ".sft", ".gguf"]


@pytest.fixture
async def farm(dsn, tmp_path):
    builds, models, keys = tmp_path / "builds", tmp_path / "models", tmp_path / "keys"
    for d in (builds / "requests", builds / "status", models, keys):
        d.mkdir(parents=True)
    app = create_app(fast_settings(dsn, builds_dir=str(builds), models_dir=str(models), keys_dir=str(keys)))
    async with app.router.lifespan_context(app):
        r = app.state.router
        for t in r.tasks:                       # the test drives the model loop's steps itself
            if t.get_coro().__name__ == "model_loop":
                t.cancel()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            yield r, c, builds, models


def builder_up(builds, models=True, formats=SAFE):
    builder.write_json(str(builds / "status" / "policy.json"), {"recipes": [], "node_sources": [], "models": models,
                                                                 "model_formats": formats})


def requests_in(builds):
    return {f.name[:-5]: json.loads(f.read_text()) for f in (builds / "requests").glob("*.json")}


async def test_models_are_checked_asked_of_the_builder_and_followed(farm):
    r, c, builds, models = farm
    follow_builder = r.models_step[0]
    one = {"url": "https://huggingface.co/Comfy-Org/flux/blob/main/split_files/flux%20dev.safetensors",
           "folder": "diffusion_models"}
    assert (await c.post("/api/admin/models", json={"models": [one]}, headers=H)).status_code == 503     # no builder
    builder_up(builds, models=False)
    assert "no model store" in (await c.post("/api/admin/models", json={"models": [one]}, headers=H)).json()["detail"]
    builder_up(builds)
    gid, _ = await add_worker(r, "http://192.0.2.21:8188", group="video")
    for bad, why in (({**one, "url": "https://example.com/a/b/blob/main/x.safetensors"}, "not a Hugging Face file link"),
                     ({**one, "url": one["url"].replace(".safetensors", ".ckpt")}, "can run code"),
                     ({**one, "folder": "custom_nodes"}, "folder: one of"),
                     ({**one, "filename": "../x.safetensors"}, "file name"),
                     ({"repo": "a/b", "path": "../../etc/passwd.safetensors", "folder": "vae"}, "file:"),
                     ({"repo": "a/b", "path": "x.safetensors", "ref": "--upload-pack=x", "folder": "vae"}, "revision")):
        got = await c.post("/api/admin/models", json={"models": [bad]}, headers=H)
        assert got.status_code == 400 and why in got.json()["detail"], (bad, got.json())
    got = await c.post("/api/admin/models", json={"models": [one, {**one, "url": one["url"] + "?download=true"}]}, headers=H)
    assert got.status_code == 400 and got.json()["detail"].startswith("line 2: diffusion_models/flux dev.safetensors twice")
    assert (await c.post("/api/admin/models", json={"models": [one], "groups": [999]}, headers=H)).status_code == 400
    assert requests_in(builds) == {}                                 # nothing refused reached the builder
    # a pasted list: links as copied from the site, or repository/file; each its own builder request
    vae = {"repo": "Comfy-Org/flux", "path": "split_files/vae/ae.safetensors", "ref": "v1.0", "folder": "vae",
           "filename": "flux-ae.safetensors"}
    assert len((await c.post("/api/admin/models", json={"models": [one, vae], "groups": [gid]}, headers=H)).json()["ids"]) == 2
    reqs = sorted(requests_in(builds).values(), key=lambda x: x["folder"])
    assert reqs == [{"kind": "model", "repo": "Comfy-Org/flux", "path": "split_files/flux dev.safetensors", "ref": "main",
                     "folder": "diffusion_models", "filename": "flux dev.safetensors", "by": "admin"},
                    {"kind": "model", "repo": "Comfy-Org/flux", "path": "split_files/vae/ae.safetensors", "ref": "v1.0",
                     "folder": "vae", "filename": "flux-ae.safetensors", "by": "admin"}]
    assert (await c.post("/api/admin/models", json={"models": [one]}, headers=H)).status_code == 409
    listed = (await c.get("/api/admin/models", headers=H)).json()
    assert [(m["filename"], m["state"], m["groups"]) for m in listed["models"]] == [
        ("flux dev.safetensors", "queued", [gid]), ("flux-ae.safetensors", "queued", [gid])]
    assert listed["builder"]["formats"] == SAFE and listed["builder"]["store"]
    # what the builder writes back: running → downloading with bytes, done → ready with its key, failed → its error
    rid = {m["filename"]: m for m in listed["models"]}
    async with r.db.conn() as x:
        req = {row["filename"]: row["request"] for row in await x.fetch("SELECT filename, request FROM models")}
    flux, ae = req["flux dev.safetensors"], req["flux-ae.safetensors"]
    builder.write_json(str(builds / "status" / f"{flux}.json"), {"id": flux, "kind": "model", "state": "running",
                                                                 "bytes": 5 << 30, "total": 23 << 30})
    builder.write_json(str(builds / "status" / f"{ae}.json"), {"id": ae, "kind": "model", "state": "failed",
                                                               "error": "Comfy-Org/flux is gated or private: …"})
    assert await follow_builder() is False
    got = {m["filename"]: m for m in (await c.get("/api/admin/models", headers=H)).json()["models"]}
    assert (got["flux dev.safetensors"]["state"], got["flux dev.safetensors"]["bytes"], got["flux dev.safetensors"]["size"]) \
        == ("downloading", 5 << 30, 23 << 30)
    assert got["flux-ae.safetensors"]["state"] == "failed" and "gated" in got["flux-ae.safetensors"]["error"]
    key = fetch.model_key("diffusion_models", "flux dev.safetensors", "e" * 64)
    builder.write_json(str(builds / "status" / f"{flux}.json"), {
        "id": flux, "kind": "model", "state": "done", "key": key, "sha256": "e" * 64, "size": 23 << 30,
        "revision": "c" * 40, "bytes": 23 << 30, "total": 23 << 30})
    assert await follow_builder() is True                             # a model became ready: hosts are asked now
    got = {m["filename"]: m for m in (await c.get("/api/admin/models", headers=H)).json()["models"]}
    assert (got["flux dev.safetensors"]["state"], got["flux dev.safetensors"]["key"], got["flux dev.safetensors"]["revision"]) \
        == ("ready", key, "c" * 40)
    # retry: a new request for the failed one only; a ready one is not removed, a failed one is
    assert (await c.post(f"/api/admin/models/{rid['flux dev.safetensors']['id']}/retry", headers=H)).status_code == 409
    again = (await c.post(f"/api/admin/models/{rid['flux-ae.safetensors']['id']}/retry", headers=H)).json()["id"]
    assert again != ae and requests_in(builds)[again]["filename"] == "flux-ae.safetensors"
    os.remove(builds / "requests" / f"{again}.json")                 # the builder took it and lost its status
    await follow_builder()
    lost = next(m for m in (await c.get("/api/admin/models", headers=H)).json()["models"] if m["id"] == rid["flux-ae.safetensors"]["id"])
    assert lost["state"] == "failed" and "no record" in lost["error"]
    assert (await c.delete(f"/api/admin/models/{rid['flux-ae.safetensors']['id']}", headers=H)).status_code == 200
    # groups and note
    g2, _ = await add_worker(r, "http://192.0.2.22:8188", group="stills")
    mid = rid["flux dev.safetensors"]["id"]
    assert (await c.patch(f"/api/admin/models/{mid}", json={"groups": [g2], "note": "dev"}, headers=H)).status_code == 200
    got = (await c.get("/api/admin/models", headers=H)).json()["models"]
    assert [(m["groups"], m["note"]) for m in got] == [([g2], "dev")]
    assert (await c.get("/api/admin/models", headers={"X-ComfyFarm-User": "mallory"})).status_code == 403
    assert (await c.post("/api/admin/models", json={"models": [vae]}, headers={"X-ComfyFarm-User": "mallory"})).status_code == 403


def store_model(models, key_file, data, folder="checkpoints", filename="tiny.safetensors"):
    """What the builder leaves in the model store: the file, read-only, and its signed entry."""
    sha = hashlib.sha256(data).hexdigest()
    (models / folder).mkdir(parents=True, exist_ok=True)
    (models / folder / filename).write_bytes(data)
    entry = fetch.model_entry(folder, filename, len(data), sha)
    key = json.loads(entry)["key"]
    lst = models / "signed" / "model" / f"{key}.json"
    lst.parent.mkdir(parents=True, exist_ok=True)
    lst.write_bytes(entry)
    fetch.sign(str(lst), str(key_file))
    return key


async def test_the_router_hands_on_models_to_its_hosts_only(farm, tmp_path):
    r, c, builds, models = farm
    key_file = tmp_path / "bkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)], check=True)
    data = os.urandom(300_000)
    key = store_model(models, key_file, data)
    _, wid = await add_worker(r, "http://192.0.2.21:8188", name="gpu-a")
    async with r.db.conn() as x:
        await x.execute("INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, "
                        "restart_backend, restart_config) VALUES ($1, 'gpu-a', 'gpu', 'posix', $2, '0.0.0.0', 8188, "
                        "'{}'::jsonb, 'manual', '{}'::jsonb)", wid, ["127.0.0.1"])
    got = (await c.get(f"/api/worker_models/{key}/signed")).json()
    signers = tmp_path / "allowed"
    signers.write_text(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {(tmp_path / "bkey.pub").read_text()}')
    fetch.verify(got["entry"].encode(), got["sig"].encode(), str(signers))
    whole = await c.get(f"/api/worker_models/{key}/file")
    assert whole.status_code == 200 and whole.content == data
    rest = await c.get(f"/api/worker_models/{key}/file", headers={"Range": "bytes=100000-"})
    assert rest.status_code == 206 and rest.content == data[100_000:]           # an interrupted copy goes on
    for bad in (f"/api/worker_models/{'0' * 32}/file", "/api/worker_models/..%2F..%2Fetc/signed",
                "/api/worker_models/tiny.safetensors/file"):
        assert (await c.get(bad)).status_code == 404, bad
    # a link in the store to a file outside it is not followed out
    other = os.urandom(1000)
    k2 = store_model(models, key_file, other, filename="link.safetensors")
    (tmp_path / "outside").write_bytes(other)
    os.remove(models / "checkpoints" / "link.safetensors")
    os.symlink(tmp_path / "outside", models / "checkpoints" / "link.safetensors")
    assert (await c.get(f"/api/worker_models/{k2}/file")).status_code == 404
    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=c._transport.app, client=("192.0.2.99", 4000)),
                                 base_url="http://farm")
    async with stranger:
        assert (await stranger.get(f"/api/worker_models/{key}/file")).status_code == 403
        assert (await stranger.get(f"/api/worker_models/{key}/signed")).status_code == 403


class FakeHost(be.Backend):
    """A host's agent as the ssh backend reads it: fetch-status per name, fetch records what is asked for."""

    def __init__(self, states):
        super().__init__({})
        self.states, self.asked, self.looked, self.forgot, self.old = states, [], 0, [], False

    async def fetch(self, artifacts, state=False):
        if state:
            self.looked += 1
            return {a: dict(self.states.get(a, {"state": "missing"})) for a in artifacts}
        self.asked.append(list(artifacts))
        for a in artifacts:
            self.states[a] = {"state": "queued"}
        return {"requested": True}

    async def forget(self, names):
        self.forgot.append(list(names))
        if self.old:
            return None                                   # worker tools before 0.1.0-dev.19
        for n in names:
            self.states.pop(n, None)
        return {"requested": True}


async def test_hosts_get_copies_of_their_groups_models_in_the_background(farm, monkeypatch):
    r, c, builds, models = farm
    _, follow_copies, copies = r.models_step
    gid, wid = await add_worker(r, "http://192.0.2.21:8188", group="video", name="gpu-a")
    _, mounted = await add_worker(r, "http://192.0.2.22:8188", group="video", name="gpu-share")
    async with r.db.conn() as x:
        for w, paths in ((wid, {"model_store": "/var/lib/comfyfarm-models"}), (mounted, {})):
            await x.execute("INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, "
                            "restart_backend, restart_config) VALUES ($1, $2, 'gpu', 'posix', $3, '0.0.0.0', 8188, "
                            "$4, 'ssh', '{}'::jsonb)", w, f"i{w}", ["192.0.2.21"], paths)
        ready = await x.fetchval("INSERT INTO models(folder, filename, repo, path, state, key) VALUES "
                                 "('vae', 'ae.safetensors', 'a/b', 'ae.safetensors', 'ready', $1) RETURNING id", "1" * 32)
        lora = await x.fetchval("INSERT INTO models(folder, filename, repo, path, state, key) VALUES "
                                "('loras', 'l.safetensors', 'a/b', 'l.safetensors', 'ready', $1) RETURNING id", "2" * 32)
        await x.execute("INSERT INTO models(folder, filename, repo, path, state) VALUES ('vae', 'x.safetensors', 'a/b', "
                        "'x.safetensors', 'downloading')")
        for m in (ready, lora):
            await x.execute("INSERT INTO group_models(group_id, model_id) VALUES ($1, $2)", gid, m)
    host = FakeHost({f"model/{'2' * 32}": {"state": "failed", "error": "needs 20.0 GB more"}})
    made, refreshed = [], []

    async def refresh(gid, prefer=None):
        refreshed.append((gid, prefer))
    monkeypatch.setattr(r, "refresh_snapshot", refresh)

    def make(name, config, keys_dir=None):
        made.append(name)
        return host
    monkeypatch.setattr(be, "make", make)
    await follow_copies()
    assert made == ["ssh"]                                            # the host with a mounted models dir is not asked
    assert host.asked == [[f"model/{'1' * 32}", f"model/{'2' * 32}"]]    # the missing one, and one that failed before
    st = next(w for w in (await c.get("/api/admin/models", headers=H)).json()["workers"] if w["id"] == wid)
    assert (st["wanted"], st["present"], st["copying"]) == (2, 0, 2) and st["failed"] == {f"model/{'2' * 32}": "needs 20.0 GB more"}
    assert next(w for w in (await c.get("/api/admin/models", headers=H)).json()["workers"] if w["id"] == mounted)["copies"] is False
    host.states[f"model/{'1' * 32}"] = {"state": "running", "bytes": 1 << 30, "total": 3 << 30}
    host.states[f"model/{'2' * 32}"] = {"state": "failed", "error": "needs 20.0 GB more"}       # failed again
    await follow_copies()
    assert len(host.asked) == 1 and (copies[wid]["bytes"], copies[wid]["total"]) == (1 << 30, 3 << 30)   # not asked again yet
    assert copies[wid]["failed"]
    from comfyfarm.api import models as models_api
    monkeypatch.setattr(models_api, "RETRY_FAILED", 0.0)              # some time later: the failed copy is asked again
    host.states[f"model/{'1' * 32}"] = {"state": "present"}
    assert refreshed == []
    await follow_copies()
    assert host.asked[-1] == [f"model/{'2' * 32}"] and copies[wid]["present"] == 1
    assert refreshed == [(gid, [wid])]                                # the editor's model lists: read again
    # an agent that keeps no copies, an agent that cannot be reached: said, not raised
    host.fetch = lambda *a, **k: _none()
    await follow_copies()
    assert "keeps no copies" in copies[wid]["error"]

    async def unreachable(*a, **k):
        raise be.BackendError("ssh: connect to host 192.0.2.21 port 22: No route to host")
    host.fetch = unreachable
    await follow_copies()
    assert "No route to host" in copies[wid]["error"]


async def _none():
    return None


async def test_several_models_go_into_groups_and_out_at_once(farm):
    """Field (dev.18): an inbox of many files, each needing its own Groups dialog."""
    r, c, builds, models = farm
    g1, _ = await add_worker(r, "http://192.0.2.21:8188", group="video")
    g2, _ = await add_worker(r, "http://192.0.2.22:8188", group="stills")
    async with r.db.conn() as x:
        ids = [await x.fetchval("INSERT INTO models(folder, filename, source, state, key) VALUES ('loras', $1, 'inbox', "
                                "'ready', $2) RETURNING id", f"l{i}.safetensors", f"{i}" * 32) for i in range(1, 4)]
        await x.execute("INSERT INTO group_models(group_id, model_id) VALUES ($1, $2)", g2, ids[0])
    bulk = lambda body, who="admin": c.post("/api/admin/models/groups", json=body, headers={"X-ComfyFarm-User": who})  # noqa: E731
    got = (await bulk({"models": ids, "add": [g1, g2]})).json()
    assert got == {"models": 3, "added": 5, "removed": 0}                       # one of the six was there already
    groups = lambda: {m["id"]: m["groups"] for m in listed}                     # noqa: E731
    listed = (await c.get("/api/admin/models", headers=H)).json()["models"]
    assert groups() == {i: sorted([g1, g2]) for i in ids}
    assert (await bulk({"models": ids[:2], "remove": [g2]})).json()["removed"] == 2
    listed = (await c.get("/api/admin/models", headers=H)).json()["models"]
    assert groups() == {ids[0]: [g1], ids[1]: [g1], ids[2]: sorted([g1, g2])}
    for body, code in (({"models": ids, "add": [g1], "remove": [g1]}, 400), ({"models": ids}, 400), ({"models": [], "add": [g1]}, 400),
                       ({"models": ids, "add": [99999]}, 400), ({"models": ids + [99999], "add": [g1]}, 404)):
        assert (await bulk(body)).status_code == code, body
    assert (await bulk({"models": ids, "add": [g2]}, who="mallory")).status_code == 403
    actions = [a["action"] for a in (await c.get("/api/admin/audit", headers=H)).json()]
    assert actions.count("models' groups changed") == 2                         # one entry a change, not one a model
