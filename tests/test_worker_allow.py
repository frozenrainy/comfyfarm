"""Worker source allow-list (docs/design/09): the extension's policy on its own, and the router's warnings."""
import asyncio
import importlib.util
import pathlib
import types

import httpx
import pytest

from comfyfarm.app import create_app
from conftest import add_worker, fast_settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("allow", ROOT / "worker_ext" / "comfyfarm_worker" / "allow.py")
allow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(allow)


def test_policy_decisions():
    p = allow.Policy("10.0.0.5, 10.0.1.0/24,  ::ffff:10.0.0.6")
    assert p.secured and p.allows("10.0.0.5") and p.allows("10.0.1.77") and p.allows("10.0.0.6")
    assert p.allows("::ffff:10.0.0.5")                         # v4-mapped v6 socket on a dual-stack listener
    assert p.allows("127.0.0.1") and p.allows("::1")           # loopback: local administration always works
    assert not p.allows("10.0.2.1") and not p.allows("192.168.1.1") and not p.allows(None) and not p.allows("garbage")
    # unset = open, and says so
    o = allow.Policy(None)
    assert not o.secured and o.allows("192.168.1.1") and o.describe() == {"secured": False, "allow": [], "invalid": []}
    # a typo must not silently open the worker while looking configured
    t = allow.Policy("10.0.0.5, not-an-address")
    assert not t.secured and t.describe()["invalid"] == ["not-an-address"]


async def test_middleware_uses_the_peer_address_not_headers():
    mw = allow.make_middleware(allow.Policy("10.0.0.5"))
    calls = []

    async def handler(request):
        calls.append(request.peer)
        return "ok"

    def req(peer, headers=None):
        r = types.SimpleNamespace(peer=peer, headers=headers or {})
        r.transport = types.SimpleNamespace(get_extra_info=lambda k: (peer, 12345) if peer else None)
        return r
    assert await mw(req("10.0.0.5"), handler) == "ok"
    assert await mw(req("127.0.0.1"), handler) == "ok"
    refused = await mw(req("192.168.1.9", {"X-Forwarded-For": "10.0.0.5"}), handler)
    assert refused.status == 403 and calls == ["10.0.0.5", "127.0.0.1"]
    assert (await mw(req(None), handler)).status == 403


async def test_router_warns_about_open_workers(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        plain, open_, locked = await fakes(), await fakes(secured=False, ws_fix=True), await fakes(secured=True, ws_fix=True)
        for h, name in ((plain, "plain"), (open_, "open"), (locked, "locked")):
            await add_worker(r, h.url, name=name)
        for _ in range(50):
            await r.health.tick()
            async with r.db.conn() as c:
                n = await c.fetchval("SELECT count(*) FROM workers WHERE capabilities ? 'extension'")
            if n == 3:
                break
            await asyncio.sleep(0.1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            ws = {w["name"]: w["warnings"] for w in (await c.get("/api/admin/workers", headers={"X-ComfyFarm-User": "admin"})).json()}
    assert any("extension not installed" in x for x in ws["plain"])
    assert any("unsecured" in x for x in ws["open"]) and not any("reconnect" in x for x in ws["open"])
    assert not any("unsecured" in x or "extension" in x for x in ws["locked"]), ws["locked"]


def test_extension_names_custom_nodes_that_did_not_import(tmp_path, monkeypatch):
    """ComfyUI keeps only the loaded nodes; the extension reports the rest (a failed import leaves the worker running
    with a shorter node list — the rollout must not call that a success)."""
    import sys
    pkg = ROOT / "worker_ext" / "comfyfarm_worker"
    spec = importlib.util.spec_from_file_location("comfyfarm_worker_under_test", pkg / "__init__.py",
                                                  submodule_search_locations=[str(pkg)])
    ext = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, ext)
    spec.loader.exec_module(ext)                     # outside ComfyUI its installs log and skip
    a, b = tmp_path / "set-a", tmp_path / "set-b"
    for d in ("GoodNode", "BrokenNode", "__pycache__", "Off.disabled"):
        (a / d).mkdir(parents=True)
    (a / "README.md").write_text("")
    b.mkdir()
    (b / "single.py").write_text("")
    (b / "single_bad.py").write_text("")
    monkeypatch.setitem(sys.modules, "nodes", types.SimpleNamespace(
        LOADED_MODULE_DIRS={"GoodNode": str(a / "GoodNode"), str(b / "single"): str(b)}))   # ComfyUI's own keys
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_folder_paths=lambda kind: [str(a), str(b), str(tmp_path / "gone")]))
    assert ext.custom_node_status() == {"loaded": ["GoodNode", "single.py"], "failed": ["BrokenNode", "single_bad.py"]}


def test_failed_custom_nodes_are_a_worker_warning():
    from comfyfarm.api.admin import CUSTOM_NODES_FAILED, security_warnings
    ext = {"installed": True, "secured": True, "ws_fix": True}
    assert security_warnings({"extension": {**ext, "custom_nodes": {"loaded": ["a"], "failed": ["X", "Y"]}}}) == \
        [CUSTOM_NODES_FAILED + "X, Y"]
    for quiet in ({"loaded": ["a"], "failed": []}, None):                 # none failed / an older extension
        assert security_warnings({"extension": {**ext, "custom_nodes": quiet}}) == []
