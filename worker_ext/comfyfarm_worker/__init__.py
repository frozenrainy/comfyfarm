# SPDX-License-Identifier: GPL-3.0-or-later
"""ComfyFarm worker extension — three things a farm needs from a stock ComfyUI worker, without patching its core:
a source allow-list (allow.py; the worker is otherwise open to everyone on the network), a validation endpoint, and
a fix for the WebSocket reconnect race.

ComfyUI's /ws handler removes `sockets[sid]` in `finally` without checking that the entry is still its own socket.
When a client reconnects with the same clientId, the old connection's cleanup deletes the new registration and every
later event for that client is dropped silently. Custom nodes load before PromptServer registers its routes, so this
module swaps the /ws route for a copy whose cleanup only removes its own socket.

The swap happens only if the original handler's source matches a known version; otherwise the original is kept and a
warning is logged (a silently wrong copy would be worse than the known bug)."""
import hashlib, inspect, json, logging, os, uuid

import aiohttp
from aiohttp import web

from .allow import from_env, make_middleware

EXT_VERSION = "0.3"

NODE_CLASS_MAPPINGS = {}
log = logging.getLogger("comfyfarm.worker")

# sha256 of inspect.getsource(original handler) for versions whose logic the copy below reproduces
KNOWN_HANDLER_SOURCES = {
    "0.41.0": "a4a525bd8f70d971d3de7c06ebb97162dbe21d11d395ca680446a2c9f56a9660",
}


# Derived from ComfyUI's server.py websocket_handler (Copyright comfyanonymous and contributors, GPL-3.0);
# the only change is the guarded cleanup in `finally`. Hence this extension is GPL-3.0 (see LICENSE here).
def _guarded_handler(server, feature_flags):
    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sid = request.rel_url.query.get("clientId", "")
        if sid:
            server.sockets.pop(sid, None)
        else:
            sid = uuid.uuid4().hex
        server.sockets[sid] = ws
        server.sockets_metadata[sid] = {"feature_flags": {}}
        try:
            await server.send("status", {"status": server.get_queue_info(), "sid": sid}, sid)
            if server.client_id == sid and server.last_node_id is not None:
                await server.send("executing", {"node": server.last_node_id}, sid)
            first_message = True
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    logging.warning("ws connection closed with exception %s" % ws.exception())
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if first_message and data.get("type") == "feature_flags":
                            if server.sockets.get(sid) is ws:
                                server.sockets_metadata[sid]["feature_flags"] = data.get("data", {})
                            await server.send("feature_flags", feature_flags.get_server_features(), sid)
                        first_message = False
                    except json.JSONDecodeError:
                        logging.warning(f"Invalid JSON received from client {sid}: {msg.data}")
                    except Exception as e:
                        logging.error(f"Error processing WebSocket message: {e}")
        finally:
            if server.sockets.get(sid) is ws:          # the fix: only remove our own registration
                server.sockets.pop(sid, None)
                server.sockets_metadata.pop(sid, None)
        return ws
    return websocket_handler


def _install():
    from server import PromptServer
    from comfy_api import feature_flags
    import comfyui_version
    server = PromptServer.instance
    items = server.routes._items
    idx = [i for i, r in enumerate(items) if getattr(r, "path", None) == "/ws" and getattr(r, "method", "") == "GET"]
    if len(idx) != 1:
        log.warning("comfyfarm: /ws route not found exactly once (%d) — reconnect fix NOT installed", len(idx))
        return
    orig = items[idx[0]]
    digest = hashlib.sha256(inspect.getsource(orig.handler).encode()).hexdigest()
    known = KNOWN_HANDLER_SOURCES.get(comfyui_version.__version__)
    if known is None or known != digest:
        log.warning("comfyfarm: /ws handler source sha256=%s for ComfyUI %s is not a known version — reconnect fix NOT installed",
                    digest, comfyui_version.__version__)
        return
    items[idx[0]] = web.RouteDef(orig.method, orig.path, _guarded_handler(server, feature_flags), orig.kwargs)
    log.info("comfyfarm: /ws reconnect fix installed (ComfyUI %s, handler %s)", comfyui_version.__version__, digest[:12])


def _install_validate():
    """POST /comfyfarm/validate {prompt, partial_execution_targets?} → 200 {ok} | 400 {error, node_errors} in the
    stock /prompt error shape, without queueing. The router validates on a worker of the target group before
    accepting a job (docs/design D-24); `execution.validate_prompt` is internal API, so the rollout smoke test sends a
    deliberately broken workflow to catch signature changes."""
    from server import PromptServer
    import execution

    @PromptServer.instance.routes.post("/comfyfarm/validate")
    async def comfyfarm_validate(request):
        body = await request.json()
        try:
            valid = await execution.validate_prompt(str(uuid.uuid4()), body.get("prompt") or {},
                                                    body.get("partial_execution_targets"))
        except Exception as e:
            return web.json_response({"error": {"type": "validator_error", "message": repr(e)}}, status=500)
        if valid[0]:
            return web.json_response({"ok": True, "node_errors": valid[3]})
        return web.json_response({"error": valid[1], "node_errors": valid[3]}, status=400)
    log.info("comfyfarm: /comfyfarm/validate installed")


def custom_node_status() -> dict:
    """The custom node directories ComfyUI tried to load, and those that did not load. ComfyUI remembers only the
    loaded ones (nodes.LOADED_MODULE_DIRS); a failed import is logged as IMPORT FAILED and otherwise forgotten, and the
    worker runs on without that node. Same walk as nodes.init_external_custom_nodes; asked at request time, after every
    custom node has had its turn."""
    import folder_paths, nodes
    loaded_dirs = {os.path.realpath(d) for d in nodes.LOADED_MODULE_DIRS.values()}
    loaded, failed = [], []
    for base in folder_paths.get_folder_paths("custom_nodes"):
        try:
            entries = sorted(os.listdir(os.path.realpath(base)))
        except OSError:
            continue
        for name in entries:
            path = os.path.join(base, name)
            if name == "__pycache__" or name.endswith(".disabled") or (os.path.isfile(path) and not name.endswith(".py")):
                continue
            ok = (os.path.realpath(path) in loaded_dirs if os.path.isdir(path)
                  else os.path.splitext(path)[0] in nodes.LOADED_MODULE_DIRS)          # a single-file node: keyed by path
            (loaded if ok else failed).append(name)
    return {"loaded": loaded, "failed": failed}


def _install_allow():
    """Refuse requests from anyone but the router/gateway (COMFYFARM_ALLOW) and report the state on
    GET /comfyfarm/info. The middleware is appended before the app starts (custom nodes import first), so it covers
    every route including /ws. An unset or malformed allow-list leaves the worker open and says so."""
    from server import PromptServer
    import comfyui_version
    server = PromptServer.instance
    policy = from_env()
    if policy.secured:
        server.app.middlewares.append(make_middleware(policy))
        log.info("comfyfarm: requests accepted only from %s (and loopback)", ", ".join(policy.describe()["allow"]))
    elif policy.bad:
        log.warning("comfyfarm: COMFYFARM_ALLOW has invalid entries %s — worker left OPEN", policy.bad)
    else:
        log.warning("comfyfarm: COMFYFARM_ALLOW not set — this worker accepts requests from any address")
    ws_fix = any(getattr(r, "path", None) == "/ws" and r.handler.__name__ == "websocket_handler" and
                 r.handler.__module__ == __name__ for r in server.routes._items)

    @server.routes.get("/comfyfarm/info")
    async def comfyfarm_info(request):
        try:
            nodes_state = custom_node_status()
        except Exception as e:                                   # never let the report break the info route
            log.warning("comfyfarm: custom node status unavailable: %r", e)
            nodes_state = None
        return web.json_response({"extension": EXT_VERSION, "comfyui": comfyui_version.__version__, "ws_fix": ws_fix,
                                  "custom_nodes": nodes_state, **policy.describe()})


for _step, _fn in (("reconnect fix", _install), ("validate endpoint", _install_validate), ("allow-list", _install_allow)):
    try:
        _fn()
    except Exception as e:  # never break worker startup
        log.warning("comfyfarm: %s NOT installed: %r", _step, e)
