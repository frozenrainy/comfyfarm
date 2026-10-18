"""Run a real ComfyFarm stack for the browser E2E: Postgres (pgserver) + router + gateway.
UI worker and GPU worker are given as URLs.  python spikes/e2e_stack.py --gpu http://gpu-worker:8188 --ui http://127.0.0.1:18188"""
import argparse, asyncio, os, pathlib

import uvicorn

from comfyfarm.app import create_app
from comfyfarm.cli import add_user
from comfyfarm.core.settings import Settings
from comfyfarm.gateway import GatewaySettings, create_gateway

ap = argparse.ArgumentParser()
ap.add_argument("--gpu", required=True); ap.add_argument("--ui", required=True)
ap.add_argument("--router-port", type=int, default=18300); ap.add_argument("--gateway-port", type=int, default=18400)
ap.add_argument("--gpu2", help="optional second worker, put into a group 'rocm' that requires backend rocm")
ap.add_argument("--rollout-demo", type=int, default=0, metavar="N",
                help="N in-process fake hosts (ports 18511…) restarted like the launcher does, with profiles 'stable', "
                     "'next' and 'next-broken' (its venv is missing on the hosts' disk: the launcher refuses → revert)")
ap.add_argument("--router-host", default="127.0.0.1", help="0.0.0.0 when launchers on other hosts fetch renders")
ap.add_argument("--gateway-host", default="127.0.0.1", help="0.0.0.0 to open the gateway to the LAN (the router stays on loopback)")
a = ap.parse_args()
SECRET, KEY = "e2e-gateway-secret", b"e2e-session-key-0123456789abcdef"


async def main():
    import pgserver, asyncpg
    srv = pgserver.get_server(pathlib.Path("lab/pgdata-e2e").resolve(), cleanup_mode=None)
    uri = srv.get_uri()
    admin = await asyncpg.connect(uri)
    await admin.execute("DROP DATABASE IF EXISTS e2e WITH (FORCE)"); await admin.execute("CREATE DATABASE e2e")
    await admin.close()
    dsn = uri.replace("/postgres?", "/e2e?")
    art = pathlib.Path("lab/e2e-art").resolve()
    app = create_app(Settings(dsn=dsn, gateway_secret=SECRET, default_group="lab", instance="e2e",
                              keys_dir=str(pathlib.Path(__file__).resolve().parent.parent / "lab" / "keys"),
                              artifacts_dir=str(art) if a.rollout_demo else None))
    gw = create_gateway(GatewaySettings(f"http://127.0.0.1:{a.router_port}", a.ui.rstrip("/"), SECRET, KEY))
    rs = uvicorn.Server(uvicorn.Config(app, host=a.router_host, port=a.router_port, log_level="warning"))
    gs = uvicorn.Server(uvicorn.Config(gw, host=a.gateway_host, port=a.gateway_port, log_level="warning"))
    t1, t2 = asyncio.create_task(rs.serve()), asyncio.create_task(gs.serve())
    while not (rs.started and gs.started):
        await asyncio.sleep(0.1)
    router = app.state.router
    await add_user(dsn, "farmadmin", "farm-e2e", admin=True)
    async with router.db.conn() as c:
        gid = await c.fetchval("INSERT INTO worker_groups(name) VALUES ('lab') RETURNING id")
        await c.execute("INSERT INTO workers(name, base_url, group_id) VALUES ('r9700', $1, $2)", a.gpu.rstrip("/"), gid)
        if a.gpu2:
            g2 = await c.fetchval("""INSERT INTO worker_groups(name, requirements) VALUES ('rocm', '{"backend": ["rocm"]}') RETURNING id""")
            await c.execute("INSERT INTO workers(name, base_url, group_id) VALUES ('rocm-1', $1, $2)", a.gpu2.rstrip("/"), g2)
    if a.rollout_demo:
        await rollout_demo(router, a.router_port, art, gid, a.rollout_demo)
    print(f"READY gateway http://{a.gateway_host}:{a.gateway_port}  router http://{a.router_host}:{a.router_port}", flush=True)
    await asyncio.gather(t1, t2)

async def rollout_demo(router, router_port, art, gid, n):
    import json, shutil
    import httpx
    from comfyfarm.rollout import render as rd
    from comfyfarm.testing.fake_host import FakeHost, FakeHostBackend
    host_art = art.parent / "e2e-art-host"                      # the same store as the hosts see it (another mount)
    for d in (art, host_art):
        shutil.rmtree(d, ignore_errors=True)
    for kind, name, extra in [("core", "comfyui-0.41.0", {}), ("core", "comfyui-0.42.0", {}),
                              ("venv", "rocm72-a", {"accelerator": "rocm"}), ("venv", "rocm72-b", {"accelerator": "rocm"}),
                              ("venv", "rocm72-c", {"accelerator": "rocm"})]:
        for root in (art, host_art):
            if root == host_art and name == "rocm72-c":
                continue                                        # published, but not on the hosts' disk
            dd = root / kind / name
            dd.mkdir(parents=True)
            (dd / "manifest.json").write_text(json.dumps({"contract": "comfy-upgrade/1", "kind": kind, "name": name, **extra}))
            (dd / "READY").write_text("")
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{router_port}")
    hosts = {}
    async with router.db.conn() as c:
        ids = {}
        for name, core, venv in [("stable", "comfyui-0.41.0", "rocm72-a"), ("next", "comfyui-0.42.0", "rocm72-b"),
                                 ("next-broken", "comfyui-0.42.0", "rocm72-c")]:
            ids[name] = await c.fetchval("INSERT INTO profiles(name, core, venv, flags) VALUES ($1, $2, $3, $4) RETURNING id",
                                         name, core, venv, ["--preview-method", "auto"])
        for i in range(1, n + 1):
            port, inst = 18510 + i, f"farm-{i}"
            local = pathlib.Path(f"lab/e2e-local/{inst}").resolve()
            paths = {"art": str(host_art), "local": str(local)} | {k: str(local / k) for k in ("models", "input", "output", "user", "temp")}
            h = FakeHost(port, router=client, instance=inst, art=paths["art"], local=paths["local"],
                         prefixes=[paths[k] for k in ("models", "input", "output", "user", "temp")], down_seconds=2.0,
                         backend="rocm")
            wid = await c.fetchval("INSERT INTO workers(name, base_url, group_id) VALUES ($1, $2, $3) RETURNING id", inst, h.url, gid)
            await c.execute("INSERT INTO instances(worker_id, instance, hosts, port, paths, restart_backend, profile_id) "
                            "VALUES ($1, $2, $3, $4, $5, 'ssh', $6)", wid, inst, ["127.0.0.1"], port, paths, ids["stable"])
            prof = dict(await c.fetchrow("SELECT * FROM profiles WHERE id = $1", ids["stable"]))
            inst_row = dict(await c.fetchrow("SELECT * FROM instances WHERE worker_id = $1", wid))
            h.argv = rd.build(prof, inst_row)["argv"][1:]      # running the stable profile
            await h.start()
            hosts[inst] = h
    router.rollout.make_backend = lambda _name, _cfg: FakeHostBackend(hosts)
    router._demo_hosts = hosts                                 # keep them alive

asyncio.run(main())
