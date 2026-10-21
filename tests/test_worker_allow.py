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


def test_extension_reads_comfyuis_own_import_verdict(monkeypatch):
    """ComfyUI's summary after the loading loop is the verdict: a pack it dropped (an exception, no mappings, a V3
    entrypoint that failed) is "(IMPORT FAILED)" there; one it skipped on purpose (--disable-all-custom-nodes,
    policy) is not listed at all — reconstructing from LOADED_MODULE_DIRS got both wrong."""
    import logging
    import sys
    pkg = ROOT / "worker_ext" / "comfyfarm_worker"
    spec = importlib.util.spec_from_file_location("comfyfarm_worker_under_test", pkg / "__init__.py",
                                                  submodule_search_locations=[str(pkg)])
    ext = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, ext)
    spec.loader.exec_module(ext)                     # outside ComfyUI its installs log and skip; the watch is installed
    try:
        assert ext.custom_node_status() is None      # nothing seen yet: unknown, not "all fine"
        root = logging.getLogger()
        level = root.level
        root.setLevel(logging.INFO)
        logging.info("\nImport times for custom nodes:")
        logging.info("   0.0 seconds: /x/custom_nodes/websocket_image_save.py")
        logging.warning("Cannot import /x/custom_nodes/BrokenNode module for custom nodes: libGL.so.1: cannot open "
                        "shared object file: No such file or directory")
        logging.info("   1.3 seconds (IMPORT FAILED): /x/custom_nodes/BrokenNode")
        logging.info("   0.2 seconds: /x/custom_nodes/GoodNode/")
        logging.warning("IMPORT FAILED: comfy_extras/nodes_broken_builtin.py")
        logging.info("Skipping Hidden due to disable_all_custom_nodes and whitelist_custom_nodes")   # not a verdict
        root.setLevel(level)
        assert ext.custom_node_status() == {"loaded": ["GoodNode", "websocket_image_save.py"],
                                            "failed": ["BrokenNode", "nodes_broken_builtin.py"],
                                            "errors": {"BrokenNode": "libGL.so.1: cannot open shared object file: "
                                                                     "No such file or directory"}}
    finally:
        logging.getLogger().removeHandler(ext._watch)


def test_failed_custom_nodes_are_a_worker_warning():
    from comfyfarm.api.admin import CUSTOM_NODES_FAILED, security_warnings
    ext = {"installed": True, "secured": True, "ws_fix": True}
    assert security_warnings({"extension": {**ext, "custom_nodes": {"loaded": ["a"], "failed": ["X", "Y"]}}}) == \
        [CUSTOM_NODES_FAILED + "X, Y"]
    for quiet in ({"loaded": ["a"], "failed": []}, None):                 # none failed / an older extension
        assert security_warnings({"extension": {**ext, "custom_nodes": quiet}}) == []


def test_the_container_network_is_added_to_the_profiles_allow_list():
    """The UI worker container: its profile may allow the router's LAN address (made for GPU workers); the gateway
    and router reach the container on the compose network — COMFYFARM_ALLOW_NETWORK, set by its supervisor."""
    p = allow.from_env({"COMFYFARM_ALLOW": "10.0.0.5", "COMFYFARM_ALLOW_NETWORK": "172.30.0.0/16"})
    assert p.secured and p.allows("10.0.0.5") and p.allows("172.30.4.2") and not p.allows("192.168.1.9")
    assert allow.from_env({"COMFYFARM_ALLOW_NETWORK": "172.30.0.0/16"}).secured
    assert not allow.from_env({}).secured



def test_a_refused_peer_is_counted_and_shown():
    """Field (dev.10): the gateway was not in a worker's allow-list and its previews failed with 403 — the worker now
    says whom it turned away, and the dashboard shows it."""
    from comfyfarm.api.admin import security_warnings
    p = allow.Policy("10.0.0.5")
    assert "refused" not in p.describe()
    p.refuse("172.20.0.4"); p.refuse("172.20.0.4")
    d = p.describe()
    assert d["refused"]["count"] == 2 and d["refused"]["last_peer"] == "172.20.0.4"
    w = security_warnings({"extension": {"installed": True, "ws_fix": True, **d}})
    assert any("refused 2 request(s), the last from 172.20.0.4" in x and "COMFYFARM_ALLOW_NETWORK" in x for x in w)
