"""ComfyFarm worker extension (prototype) — fixes the WebSocket reconnect race without patching ComfyUI core.

ComfyUI's /ws handler removes `sockets[sid]` in `finally` without checking that the entry is still its own socket.
When a client reconnects with the same clientId, the old connection's cleanup deletes the new registration and every
later event for that client is dropped silently. Custom nodes load before PromptServer registers its routes, so this
module swaps the /ws route for a copy whose cleanup only removes its own socket.

The swap happens only if the original handler's source matches a known version; otherwise the original is kept and a
warning is logged (a silently wrong copy would be worse than the known bug)."""
import hashlib, inspect, json, logging, uuid

import aiohttp
from aiohttp import web

NODE_CLASS_MAPPINGS = {}
log = logging.getLogger("comfyfarm.worker")

# sha256 of inspect.getsource(original handler) for versions whose logic the copy below reproduces
KNOWN_HANDLER_SOURCES = {
    "0.41.0": "a4a525bd8f70d971d3de7c06ebb97162dbe21d11d395ca680446a2c9f56a9660",
}


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


try:
    _install()
except Exception as e:  # never break worker startup
    log.warning("comfyfarm: reconnect fix NOT installed: %r", e)
