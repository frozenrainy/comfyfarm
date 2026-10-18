"""Group consistency: a worker whose nodes or models differ from the rest of its group is named, with what differs."""
import asyncio
import time

import httpx

from comfyfarm.app import create_app
from comfyfarm.core import consistency
from conftest import add_worker, fast_settings

H = {"X-ComfyFarm-User": "admin"}


def test_report_picks_the_majority_and_names_the_differences():
    def w(i, gid, nodes, models, **kw):
        nh, mh = consistency.hashes(nodes, models)
        return {"id": i, "group_id": gid, "state": "online", "enabled": True, "maintenance": False,
                "capabilities": {"inventory": {"nodes": nodes, "models": models, "nodes_hash": nh, "models_hash": mh}}, **kw}
    ref_models = {"loras": ["a.safetensors", "b.safetensors"], "vae": ["v.safetensors"]}
    rows = [w(1, 1, ["A", "B"], ref_models), w(2, 1, ["A", "B"], ref_models),
            w(3, 1, ["A", "C"], {"loras": ["a.safetensors"], "vae": ["v.safetensors"], "checkpoints": ["x.ckpt"]}),
            w(4, 2, ["Z"], {}),                                                   # alone in its group: nothing to compare
            w(5, 1, ["A", "B"], ref_models, state="offline")]                      # offline: not compared
    rep = consistency.report(rows)
    g = rep[1]
    assert g["reference"] == 1 and g["inconsistent"] == 1 and g["workers"][1]["consistent"] and g["workers"][2]["consistent"]
    d = g["workers"][3]
    assert d["nodes_missing"] == ["B"] and d["nodes_extra"] == ["C"]
    assert d["models_missing"] == {"loras": ["b.safetensors"]} and d["models_extra"] == {"checkpoints": ["x.ckpt"]}
    text = " | ".join(consistency.warnings(d))
    assert "1 node(s) missing — B" in text and "extra node(s) — C" in text and "missing in loras — b.safetensors" in text
    assert rep[2]["inconsistent"] == 0 and 5 not in g["workers"]
    assert consistency.warnings(g["workers"][1]) == []
    # a tie between two workers: the lowest id is the reference, the other differs (no majority = still a finding)
    rep2 = consistency.report([w(1, 1, ["A"], {}), w(2, 1, ["B"], {})])
    assert rep2[1]["reference"] == 1 and rep2[1]["inconsistent"] == 1


async def test_router_flags_the_odd_worker_and_clears_after_refresh(dsn, fakes):
    app = create_app(fast_settings(dsn, inventory_period=1.0))
    async with app.router.lifespan_context(app):
        r = app.state.router
        same = {"loras": ["x.safetensors", "y.safetensors"]}
        a = await fakes(models=same)
        b = await fakes(models=same)
        odd = await fakes(models={"loras": ["x.safetensors"]}, nodes_extra=["ComfyFarmOnlyHere"])
        gid = None
        for h, name in ((a, "a"), (b, "b"), (odd, "odd")):
            gid, _ = await add_worker(r, h.url, name=name)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            async def warnings():
                return {w["name"]: w["warnings"] for w in (await c.get("/api/admin/workers", headers=H)).json()}
            for _ in range(100):
                ws = await warnings()
                if any("differs" in x for x in ws.get("odd", [])):
                    break
                await asyncio.sleep(0.1)
            assert any("missing in loras — y.safetensors" in x for x in ws["odd"]), ws["odd"]
            assert any("extra node(s) — ComfyFarmOnlyHere" in x for x in ws["odd"])
            assert not any("differs" in x for x in ws["a"] + ws["b"])
            health = (await c.get("/api/health/dispatch")).json()["groups"][str(gid)]
            assert health["inconsistent_workers"] == 1
            assert f'comfyfarm_group_inconsistent_workers{{group="{gid}"}} 1' in (await c.get("/metrics")).text
            # the operator copies the model and removes the stray node: the periodic re-read clears the warning
            odd.fake.models, odd.fake.nodes_extra = same, []
            for _ in range(100):
                ws = await warnings()
                if not any("differs" in x for x in ws["odd"]):
                    break
                await asyncio.sleep(0.2)
            assert not any("differs" in x for x in ws["odd"]), ws["odd"]
            assert (await c.get("/api/health/dispatch")).json()["groups"][str(gid)]["inconsistent_workers"] == 0


async def test_an_unanswered_inventory_is_not_asked_again_every_tick(dsn, fakes):
    """/object_info is multi-MB and read serially with a 90 s timeout: a worker whose inventory fails must not be
    re-polled every health tick — the attempt counts like an answer for inventory_period."""
    from comfyfarm.app import Router
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    try:
        w = await fakes()
        w.fake.object_info_ok = False                                 # a broken upgrade: /object_info answers 500
        await add_worker(r, w.url)
        calls = []
        real = r.adapter.inventory

        async def inventory(base_url):
            calls.append(base_url)
            return await real(base_url)
        r.adapter.inventory = inventory
        for _ in range(4):
            await r.health.tick()
        assert len(calls) == 1, calls
    finally:
        await r.stop()
