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
    app = create_app(Settings(dsn=dsn, gateway_secret=SECRET, default_group="lab", instance="e2e"))
    gw = create_gateway(GatewaySettings(f"http://127.0.0.1:{a.router_port}", a.ui.rstrip("/"), SECRET, KEY))
    rs = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.router_port, log_level="warning"))
    gs = uvicorn.Server(uvicorn.Config(gw, host="127.0.0.1", port=a.gateway_port, log_level="warning"))
    t1, t2 = asyncio.create_task(rs.serve()), asyncio.create_task(gs.serve())
    while not (rs.started and gs.started):
        await asyncio.sleep(0.1)
    router = app.state.router
    await add_user(dsn, "farmadmin", "farm-e2e", admin=True)
    async with router.db.conn() as c:
        gid = await c.fetchval("INSERT INTO worker_groups(name) VALUES ('lab') RETURNING id")
        await c.execute("INSERT INTO workers(name, base_url, group_id) VALUES ('r9700', $1, $2)", a.gpu.rstrip("/"), gid)
    print(f"READY gateway http://127.0.0.1:{a.gateway_port}  router http://127.0.0.1:{a.router_port}", flush=True)
    await asyncio.gather(t1, t2)

asyncio.run(main())
