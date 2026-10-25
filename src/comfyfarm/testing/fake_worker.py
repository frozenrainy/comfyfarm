"""Protocol-accurate fake ComfyUI worker for router tests.

It reproduces the behaviours in docs/design/04-worker-protocol.md, including the inconvenient ones:
  - client-chosen prompt_id, canonical lowercase UUID only (P1, P2)
  - re-POSTing an existing prompt_id is accepted and runs again (P3)
  - /api/jobs statuses, /history shape, /queue shape (P4)
  - 400 + node_errors for invalid workflows (P5)
  - targeted, cooperative interrupt: checked only between steps (P6, P7, P8)
  - job events only to the job's client_id socket, status broadcasts to all (P10)
  - the stock /ws reconnect race (P11) unless `ws_fix=True`
  - feature_flags handshake (P12)

A wrong fake is worse than none (it invents failures that do not exist), so tests/test_fake_parity.py runs the same
protocol probe against this fake and expects the same verdicts as against real ComfyUI.

Fault modes (POST /_fake/mode {"mode": ..., "seconds": ...}): see docs/design/11-testing.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import re
import struct
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

log = logging.getLogger("fake_worker")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Minimal node catalogue: name -> (required inputs {name: type}, output types, seconds per step, steps)
NODES = {
    "EmptyImage": ({"width": "INT", "height": "INT", "batch_size": "INT", "color": "INT"}, ["IMAGE"]),
    "ImageBlur": ({"image": "IMAGE", "blur_radius": "INT", "sigma": "FLOAT"}, ["IMAGE"]),
    "ImageInvert": ({"image": "IMAGE"}, ["IMAGE"]),
    "PreviewImage": ({"images": "IMAGE"}, []),
    "SaveImage": ({"images": "IMAGE", "filename_prefix": "STRING"}, []),
}
OUTPUT_NODES = {"PreviewImage", "SaveImage"}
OUTPUT_TYPE = {"PreviewImage": "temp", "SaveImage": "output"}     # as stock ComfyUI reports them
REAL_DEFS = json.loads((pathlib.Path(__file__).parent / "fake_object_info.json").read_text())["nodes"]
assert set(REAL_DEFS) == set(NODES), "fake_object_info.json must cover exactly the fake's nodes"


def node_cost(node: dict) -> tuple[int, float]:
    """(steps, seconds per step) — ImageBlur is the 'slow' node; big radius × big image ≈ long single op."""
    if node["class_type"] == "ImageBlur":
        r = int(node["inputs"].get("blur_radius", 1))
        return (1, 0.02 * r) if r < 20 else (1, 4.0)   # one long step: interrupt cannot cut it (P7)
    return (1, 0.005)


@dataclass
class Job:
    prompt_id: str
    number: int
    prompt: dict
    client_id: str | None
    extra: dict
    created: float = field(default_factory=time.time)
    status: str = "pending"          # pending | in_progress | completed | failed | cancelled
    started: float | None = None
    ended: float | None = None
    messages: list = field(default_factory=list)
    interrupt: bool = False


class FakeWorker:
    def __init__(self, *, version: str = "0.41.0-fake", backend: str = "cuda", ws_fix: bool = False, vram_gb: int = 16,
                 validate_endpoint: bool = True, secured: bool | None = None, models: dict | None = None,
                 nodes_extra: list[str] | None = None, extension: str = "0.3-fake"):
        # secured: None = no extension (/comfyfarm/info 404); True/False = the extension's allow-list state
        # extension: its version; from "0.9" it answers GET /comfyfarm/health (executor thread, device)
        self.version, self.backend, self.ws_fix, self.vram_gb = version, backend, ws_fix, vram_gb
        self.argv = ["main.py", "--listen", "0.0.0.0"]          # system.argv (sys.argv without the interpreter, P15)
        self.object_info_ok = True                             # False: /object_info answers 500 (a broken upgrade)
        self.validate_calls = 0
        self.sockets: dict[str, web.WebSocketResponse] = {}
        self.sockets_metadata: dict[str, dict] = {}
        self.pending: list[Job] = []
        self.running: Job | None = None
        self.history: dict[str, Job] = {}
        self.jobs: dict[str, Job] = {}
        self.counter = 0
        self.post_log: list[str] = []        # every POST /prompt prompt_id received (tests count duplicates)
        self.post_extra: list[dict] = []     # extra_data of each POST (credential forwarding tests)
        self.mode, self.mode_seconds = "normal", 0.0
        self.wake = asyncio.Event()
        self.app = web.Application(middlewares=[self._fault_middleware])
        r = self.app.router
        r.add_get("/ws", self.ws)
        for p in ("", "/api"):
            r.add_post(p + "/prompt", self.post_prompt)
            r.add_get(p + "/queue", self.get_queue)
            r.add_post(p + "/queue", self.post_queue)
            r.add_get(p + "/history", self.get_history)
            r.add_get(p + "/history/{pid}", self.get_history_one)
            r.add_post(p + "/interrupt", self.post_interrupt)
            r.add_get(p + "/object_info", self.get_object_info)
            r.add_get(p + "/system_stats", self.get_system_stats)
        r.add_get("/api/jobs", self.get_jobs)
        r.add_get("/view", self.view)
        r.add_get("/api/view", self.view)
        r.add_get("/api/jobs/{pid}", self.get_job)
        r.add_post("/api/jobs/{pid}/cancel", self.cancel_job)
        r.add_post("/_fake/mode", self.set_mode)
        if validate_endpoint:                                   # the comfyfarm_worker extension's endpoint
            r.add_post("/comfyfarm/validate", self.validate_only)
        self.secured = secured
        self.failed_nodes: list[str] = []                              # custom nodes that "did not import"
        self.models = models or {"checkpoints": [], "loras": []}       # /models, /models/{folder}
        self.nodes_extra = list(nodes_extra or [])                     # extra class names in /object_info
        for p in ("", "/api"):
            r.add_get(p + "/models", self.get_models)
            r.add_get(p + "/models/{folder}", self.get_models_folder)
        self.extension = extension
        self.gpu_fault: str | None = None     # set: the device is broken — /system_stats answers 500 (as ComfyUI does)
        if secured is not None:
            r.add_get("/comfyfarm/info", self.info)
            if tuple(int(x) for x in extension.split("-")[0].split(".")) >= (0, 9):
                r.add_get("/comfyfarm/health", self.health)
        self.app.on_startup.append(self._start)

    # ---------------- fault injection ----------------
    @web.middleware
    async def _fault_middleware(self, request, handler):
        path = request.path
        if path.startswith("/_fake"):
            return await handler(request)
        if self.mode == "block":                      # whole event loop stalls (model load, P14)
            time.sleep(self.mode_seconds)             # noqa: deliberately blocking
            self.mode = "normal"
        if self.mode == "deaf" and (path.endswith("/prompt") or "/history" in path or "/api/jobs" in path):
            await asyncio.sleep(self.mode_seconds or 3600)   # /queue stays healthy on purpose
        return await handler(request)

    async def set_mode(self, request):
        body = await request.json()
        self.mode, self.mode_seconds = body.get("mode", "normal"), float(body.get("seconds", 0))
        if self.mode == "amnesia":                   # restart: forget queue and history
            self.pending.clear(); self.history.clear(); self.jobs.clear(); self.running = None
            self.mode = "normal"
        return web.json_response({"mode": self.mode})

    # ---------------- websocket (stock semantics, P10–P12) ----------------
    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sid = request.rel_url.query.get("clientId", "")
        if sid:
            self.sockets.pop(sid, None)
        else:
            sid = uuid.uuid4().hex
        self.sockets[sid] = ws
        self.sockets_metadata[sid] = {"feature_flags": {}}
        try:
            await self.send("status", {"status": self.queue_info(), "sid": sid}, sid)
            if self.running and self.running.client_id == sid:
                await self.send("executing", {"node": None, "prompt_id": self.running.prompt_id}, sid)
            first = True
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if first and data.get("type") == "feature_flags":
                        self.sockets_metadata.get(sid, {})["feature_flags"] = data.get("data", {})
                        await self.send("feature_flags", {"supports_preview_metadata": True, "max_upload_size": 104857600}, sid)
                    first = False
        finally:
            if not self.ws_fix or self.sockets.get(sid) is ws:   # stock: unconditional pop = the reconnect race (P11)
                self.sockets.pop(sid, None)
                self.sockets_metadata.pop(sid, None)
        return ws

    async def send(self, event: str, data: dict, sid: str | None = None):
        targets = [self.sockets.get(sid)] if sid else list(self.sockets.values())
        for ws in targets:
            if ws is not None and not ws.closed:
                try:
                    await ws.send_str(json.dumps({"type": event, "data": data}))
                except ConnectionError:
                    pass

    async def send_preview(self, prompt_id: str, node_id: str, sid: str | None):
        """A tiny JPEG-tagged frame per step: type 4 with metadata if the client negotiated it, else type 1 (P12)."""
        ws = self.sockets.get(sid) if sid else None
        if ws is None or ws.closed:
            return
        image = b"\xff\xd8fake-jpeg\xff\xd9"
        if self.sockets_metadata.get(sid, {}).get("feature_flags", {}).get("supports_preview_metadata"):
            meta = json.dumps({"prompt_id": prompt_id, "node_id": node_id, "image_type": "image/jpeg"}).encode()
            frame = struct.pack(">I", 4) + struct.pack(">I", len(meta)) + meta + image
        else:
            frame = struct.pack(">I", 1) + struct.pack(">I", 1) + image
        try:
            await ws.send_bytes(frame)
        except ConnectionError:
            pass

    def queue_info(self):
        return {"exec_info": {"queue_remaining": len(self.pending) + (1 if self.running else 0)}}

    async def broadcast_status(self):
        await self.send("status", {"status": self.queue_info()})

    # ---------------- HTTP ----------------
    def validate(self, prompt: dict) -> dict:
        errors = {}
        for nid, node in prompt.items():
            ct = node.get("class_type")
            if ct not in NODES:
                errors[nid] = {"errors": [{"type": "missing_node_type", "message": f"Node '{ct}' not found"}], "class_type": ct}
                continue
            spec, _ = NODES[ct]
            for name, typ in spec.items():
                v = node.get("inputs", {}).get(name)
                if isinstance(v, list):
                    if len(v) != 2 or str(v[0]) not in prompt:
                        errors.setdefault(nid, {"errors": [], "class_type": ct})["errors"].append(
                            {"type": "bad_linked_input", "message": f"Bad linked input {name}"})
                elif typ == "INT" and not isinstance(v, int) or typ == "FLOAT" and not isinstance(v, (int, float)):
                    errors.setdefault(nid, {"errors": [], "class_type": ct})["errors"].append(
                        {"type": "invalid_input_type", "message": f"Failed to convert an input value to a {typ} value"})
        if not any(n.get("class_type") in OUTPUT_NODES for n in prompt.values()):
            return {"__prompt__": "prompt_no_outputs"}
        return errors

    async def post_prompt(self, request):
        body = await request.json()
        pid = body.get("prompt_id") or str(uuid.uuid4())
        self.post_log.append(pid)
        self.post_extra.append(body.get("extra_data", {}))
        if self.mode == "drop":
            return web.json_response({"error": {"type": "prompt_outputs_failed_validation", "message": "fake drop"},
                                      "node_errors": {"1": {"errors": [{"type": "fake"}]}}}, status=400)
        if not UUID_RE.match(pid):
            return web.json_response({"error": {"type": "invalid_prompt_id", "message": "prompt_id must be a valid UUID",
                                                "details": "prompt_id must be a UUID string in canonical lowercase hyphenated form; omit it to let the server generate one",
                                                "extra_info": {}}, "node_errors": {}}, status=400)
        prompt = body.get("prompt", {})
        errors = self.validate(prompt)
        if errors:
            et = "prompt_no_outputs" if "__prompt__" in errors else "prompt_outputs_failed_validation"
            return web.json_response({"error": {"type": et, "message": et}, "node_errors": {} if "__prompt__" in errors else errors}, status=400)
        self.counter += 1
        job = Job(pid, self.counter, prompt, body.get("client_id"), body.get("extra_data", {}))
        self.jobs[pid] = job                                   # P3: same id replaces and runs again
        self.pending.append(job)
        if self.mode == "hang":
            await asyncio.sleep(self.mode_seconds or 30)
        self.wake.set()
        await self.broadcast_status()
        return web.json_response({"prompt_id": pid, "number": job.number, "node_errors": {}})

    def _qitem(self, j: Job):
        return [j.number, j.prompt_id, j.prompt, j.extra, [k for k, n in j.prompt.items() if n["class_type"] in OUTPUT_NODES]]

    async def validate_only(self, request):
        self.validate_calls += 1
        errors = self.validate((await request.json()).get("prompt") or {})
        if errors:
            et = "prompt_no_outputs" if "__prompt__" in errors else "prompt_outputs_failed_validation"
            return web.json_response({"error": {"type": et, "message": et}, "node_errors": {} if "__prompt__" in errors else errors}, status=400)
        return web.json_response({"ok": True, "node_errors": {}})

    async def get_queue(self, request):
        return web.json_response({"queue_running": [self._qitem(self.running)] if self.running else [],
                                  "queue_pending": [self._qitem(j) for j in self.pending]})

    async def post_queue(self, request):
        body = await request.json()
        if body.get("clear"):
            self.pending.clear()
        for pid in body.get("delete", []):
            self.pending = [j for j in self.pending if j.prompt_id != pid]
            if pid in self.jobs and self.jobs[pid].status == "pending":
                del self.jobs[pid]
        await self.broadcast_status()
        return web.Response(status=200)

    def _hist(self, j: Job):
        ok = j.status == "completed"
        return {"prompt": self._qitem(j), "outputs": {k: {"images": [{"filename": f"fake_{j.prompt_id[:8]}.png", "subfolder": "",
                                                                     "type": OUTPUT_TYPE[n["class_type"]]}]}
                                                      for k, n in j.prompt.items() if n["class_type"] in OUTPUT_NODES} if ok else {},
                "status": {"status_str": "success" if ok else "error", "completed": ok, "messages": j.messages}}

    async def get_history(self, request):
        return web.json_response({pid: self._hist(j) for pid, j in self.history.items()})

    async def get_history_one(self, request):
        pid = request.match_info["pid"]
        return web.json_response({pid: self._hist(self.history[pid])} if pid in self.history else {})

    def _job_json(self, j: Job):
        ms = lambda t: int(t * 1000) if t else None
        return {"id": j.prompt_id, "status": j.status, "priority": j.number, "create_time": ms(j.created),
                "execution_start_time": ms(j.started), "execution_end_time": ms(j.ended),
                "outputs_count": 1 if j.status == "completed" else 0}

    async def get_jobs(self, request):
        return web.json_response({"jobs": [self._job_json(j) for j in sorted(self.jobs.values(), key=lambda x: -x.number)]})

    async def get_job(self, request):
        j = self.jobs.get(request.match_info["pid"])
        return web.json_response(self._job_json(j)) if j else web.json_response({"error": "not found"}, status=404)

    async def post_interrupt(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        pid = body.get("prompt_id")
        if self.running and (pid is None or pid == self.running.prompt_id):
            self.running.interrupt = True
        else:
            log.info("Prompt %s is not currently running, skipping interrupt", pid)
        return web.Response(status=200)

    async def cancel_job(self, request):
        """Idempotent: running → interrupt (cooperative), pending → removed, finished/unknown → cancelled false."""
        pid = request.match_info["pid"]
        if self.running and self.running.prompt_id == pid:
            self.running.interrupt = True
            return web.json_response({"cancelled": True})
        for j in self.pending:
            if j.prompt_id == pid:
                self.pending.remove(j)
                j.status, j.ended = "cancelled", time.time()
                await self.broadcast_status()
                return web.json_response({"cancelled": True})
        return web.json_response({"cancelled": False})

    async def view(self, request):
        """Serves the fake outputs the history lists (temp files exist only on the executing worker)."""
        name = request.query.get("filename", "")
        if not any(name == f"fake_{j.prompt_id[:8]}.png" for j in self.history.values()):
            return web.Response(status=404)
        return web.Response(body=b"\x89PNG fake " + name.encode(), content_type="image/png")

    async def get_object_info(self, request):
        if not self.object_info_ok:
            return web.json_response({"error": "node import failed"}, status=500)
        # the real definitions of the fake's nodes (current frontends drop definitions missing name/category/…)
        defs = dict(REAL_DEFS)
        for n in self.nodes_extra:
            defs[n] = {**REAL_DEFS["ImageInvert"], "name": n, "display_name": n}
        return web.json_response(defs)

    async def get_models(self, request):
        return web.json_response(sorted(self.models))

    async def get_models_folder(self, request):
        f = request.match_info["folder"]
        return web.json_response(self.models.get(f, []))

    async def health(self, request):
        executor = "dead" if self._executor.done() else "alive"
        return web.json_response({"extension": self.extension, "executor": executor,
                                  "device": f"error: AcceleratorError: {self.gpu_fault}" if self.gpu_fault else "ok"})

    def kill_executor(self):
        """The prompt executor dies, the process stays: requests are answered, nothing runs (field, dev.21)."""
        self._executor.cancel()

    def restart(self):
        """What the service manager does after the watchdog exits: a fresh process — empty queue, live executor."""
        self.pending.clear(); self.history.clear(); self.jobs.clear()
        self.running, self.gpu_fault = None, None
        if self._executor.done():
            self._executor = asyncio.create_task(self.executor())

    async def info(self, request):
        return web.json_response({"extension": self.extension, "comfyui": self.version, "ws_fix": self.ws_fix,
                                  "custom_nodes": {"loaded": ["comfyfarm_worker"], "failed": self.failed_nodes,
                                                   "errors": {n: "libGL.so.1: cannot open shared object file: No such file or "
                                                              "directory" for n in self.failed_nodes}},
                                  "secured": self.secured, "allow": ["10.0.0.1/32"] if self.secured else [], "invalid": []})

    async def get_system_stats(self, request):
        if self.gpu_fault:                    # ComfyUI asks torch for free VRAM here: a broken context raises
            return web.json_response({"error": self.gpu_fault}, status=500)
        torch = "2.14.0+rocm7.2" if self.backend == "rocm" else "2.14.0+cu130"
        return web.json_response({"system": {"comfyui_version": self.version, "pytorch_version": torch, "os": "posix",
                                             "argv": self.argv},
                                  "devices": [{"name": f"Fake {self.backend.upper()} GPU", "type": "cuda", "vram_total": self.vram_gb * 1073741824}]})

    # ---------------- executor ----------------
    async def _start(self, app):
        self._executor = asyncio.create_task(self.executor())

    async def executor(self):
        while True:
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            job = self.pending.pop(0)
            self.running, job.status, job.started = job, "in_progress", time.time()
            ts = lambda: int(time.time() * 1000)
            job.messages = [["execution_start", {"prompt_id": job.prompt_id, "timestamp": ts()}],
                            ["execution_cached", {"nodes": [], "prompt_id": job.prompt_id, "timestamp": ts()}]]
            cid = job.client_id
            await self.send("execution_start", {"prompt_id": job.prompt_id, "timestamp": ts()}, cid)
            await self.send("execution_cached", {"nodes": [], "prompt_id": job.prompt_id}, cid)
            executed, interrupted = [], None
            for nid, node in job.prompt.items():
                if job.interrupt:                                  # cooperative: checked between nodes (P7)
                    interrupted = (nid, node["class_type"]); break
                await self.send("executing", {"node": nid, "display_node": nid, "prompt_id": job.prompt_id}, cid)
                steps, per = node_cost(node)
                for s in range(steps):
                    await asyncio.sleep(per * (5 if self.mode == "slow-interrupt" and job.interrupt else 1))
                    await self.send("progress_state", {"prompt_id": job.prompt_id, "nodes": {nid: {"value": s + 1, "max": steps, "state": "running"}}}, cid)
                    await self.send_preview(job.prompt_id, nid, cid)
                if node["class_type"] in OUTPUT_NODES and self.mode != "error":
                    out = {"images": [{"filename": f"fake_{job.prompt_id[:8]}.png", "subfolder": "",
                                       "type": OUTPUT_TYPE[node["class_type"]]}]}
                    self.history[job.prompt_id] = job                # file exists from now on (served by /view)
                    await self.send("executed", {"node": nid, "display_node": nid, "output": out, "prompt_id": job.prompt_id}, cid)
                if self.mode == "error" and node["class_type"] in OUTPUT_NODES:
                    interrupted = ("error", nid, node["class_type"]); break
                executed.append(nid)
            job.ended, self.running = time.time(), None
            if interrupted and interrupted[0] == "error":
                job.status = "failed"
                ev = {"prompt_id": job.prompt_id, "node_id": interrupted[1], "node_type": interrupted[2], "executed": executed,
                      "exception_message": "fake execution error", "exception_type": "RuntimeError", "traceback": [], "timestamp": ts()}
                job.messages.append(["execution_error", ev])
                await self.send("execution_error", ev, cid)
            elif interrupted:
                job.status = "cancelled"
                ev = {"prompt_id": job.prompt_id, "node_id": interrupted[0], "node_type": interrupted[1], "executed": executed, "timestamp": ts()}
                job.messages.append(["execution_interrupted", ev])
                await self.send("execution_interrupted", ev, cid)
            else:
                job.status = "completed"
                job.messages.append(["execution_success", {"prompt_id": job.prompt_id, "timestamp": ts()}])
                await self.send("executing", {"node": None, "prompt_id": job.prompt_id}, cid)
                await self.send("execution_success", {"prompt_id": job.prompt_id, "timestamp": ts()}, cid)
            self.history[job.prompt_id] = job
            await self.broadcast_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18190)
    ap.add_argument("--backend", default="cuda")
    ap.add_argument("--ws-fix", action="store_true", help="behave as if the comfyfarm_worker extension is installed")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    web.run_app(FakeWorker(backend=a.backend, ws_fix=a.ws_fix).app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
