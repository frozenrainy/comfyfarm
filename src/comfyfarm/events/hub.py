"""Worker WebSocket → progress / preview / error relay to the submitting browser (docs/design/05-events.md)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
import uuid
from dataclasses import dataclass, field

import websockets

from ..core import jobs as jobsvc
from ..core.dispatcher import worker_client_id
from ..core.settings import Settings
from ..db import Busy, Database

log = logging.getLogger(__name__)

TERMINAL_EVENTS = {"execution_success": "completed", "execution_error": "failed", "execution_interrupted": "cancelled"}
PING_INTERVAL, PING_TIMEOUT = 20, 300          # P14: a worker loading a model must not be disconnected
PROGRESS_THROTTLE = 2.0                        # D-15
CATCH_UP_WINDOW = 60                           # D-19


@dataclass
class JobRef:
    job_id: int
    prompt_id: str
    owner: str
    browser_client_id: str | None


@dataclass
class BrowserConn:
    ws: object                                  # starlette WebSocket
    owner: str
    client_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    preview_metadata: bool = False              # browser announced supports_preview_metadata


PREVIEW_IMAGE, PREVIEW_IMAGE_WITH_METADATA, TEXT_FRAME = 1, 4, 3     # ComfyUI protocol.BinaryEventTypes
ITERATION = re.compile(r"^Iteration (\d+) / (\d+)$")     # comfy_extras/nodes_loop.py: StartLoop and LoopProgress say it
EXPANDED = re.compile(r"^.*\.\d+\.\d+\.\d+_(.+)$")        # "<start>.<call>.<graph>.<iteration>_<id>": a loop body node


def prompt_node(node_id: str) -> str:
    """The id a reported node has in the submitted prompt: a loop iteration's copy is reported as
    "96.0.0.3_160:137" — node 160:137 of the prompt, its fourth iteration (nested loops: the innermost)."""
    m = EXPANDED.match(node_id)
    return m.group(1) if m else node_id


def parse_text(frame: bytes) -> tuple[str, str] | None:
    """(node id, text) of a progress-text frame (send_progress_text: type, id length, id, utf-8 text)."""
    if len(frame) < 8 or struct.unpack(">I", frame[:4])[0] != TEXT_FRAME:
        return None
    n = struct.unpack(">I", frame[4:8])[0]
    try:
        return frame[8:8 + n].decode(), frame[8 + n:].decode()
    except UnicodeDecodeError:
        return None


class JobTrack:
    """What a running job's progress says besides the per-node numbers (design 05), kept by the stream that sees it:
    when each sampler gave its first step (its pace without the model loading before it), each loop's iteration count
    and the time each iteration ended, and every prompt node that ran or came from the cache."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.state_at: float | None = None
        self.steps: dict[str, list] = {}           # node id → [unix time, value] of its first step report
        self.loops: dict[str, dict] = {}           # start id → {"n": N, "ends": [unix time of iteration 0 start, 1 end, …]}
        self.ran: set[str] = set()

    def doc(self, state) -> dict:
        return {"state": state, "at": self.state_at, "steps": self.steps, "loops": self.loops}


def parse_preview(frame: bytes) -> tuple[dict | None, bytes]:
    """(metadata or None, frame). Type-4 frames: [4][len][metadata json][image]; metadata carries prompt_id."""
    if len(frame) >= 8 and struct.unpack(">I", frame[:4])[0] == PREVIEW_IMAGE_WITH_METADATA:
        n = struct.unpack(">I", frame[4:8])[0]
        try:
            return json.loads(frame[8:8 + n]), frame
        except ValueError:
            return None, frame
    return None, frame


def downgrade_preview(frame: bytes, meta: dict) -> bytes:
    """Type-4 → type-1 for browsers that did not negotiate preview metadata: [1][1=jpeg|2=png][image]."""
    n = struct.unpack(">I", frame[4:8])[0]
    image = frame[8 + n:]
    kind = 2 if meta.get("image_type") == "image/png" else 1
    return struct.pack(">I", PREVIEW_IMAGE) + struct.pack(">I", kind) + image


class BrowserHub:
    """Browser sockets keyed by clientId. A clientId is bound to the identity that opened it (D-28): another user
    cannot attach to it, and events are relayed only to sockets of the job's owner."""

    def __init__(self):
        self.conns: dict[str, BrowserConn] = {}
        self.tasks: set[asyncio.Task] = set()

    def bind(self, client_id: str, owner: str, ws) -> BrowserConn | None:
        cur = self.conns.get(client_id)
        if cur is not None and cur.owner != owner:
            return None
        conn = BrowserConn(ws, owner, client_id)
        self.conns[client_id] = conn
        return conn

    def unbind(self, conn: BrowserConn):
        if self.conns.get(conn.client_id) is conn:            # the reconnect-race lesson, applied to ourselves (P11)
            del self.conns[conn.client_id]

    def target(self, ref: JobRef) -> BrowserConn | None:
        c = self.conns.get(ref.browser_client_id or "")
        return c if c is not None and c.owner == ref.owner else None

    def send_preview(self, conn: BrowserConn | None, frame: bytes, meta: dict | None):
        if conn is None:
            return
        self.send(conn, frame if (meta is None or conn.preview_metadata) else downgrade_preview(frame, meta))

    def send(self, conn: BrowserConn | None, message):
        """Fire-and-forget with the target captured by value now (D-16); per-socket lock keeps text/binary order."""
        if conn is None:
            return
        async def go():
            async with conn.lock:
                try:
                    if isinstance(message, (bytes, bytearray)):
                        await conn.ws.send_bytes(bytes(message))
                    else:
                        await conn.ws.send_text(json.dumps(message))
                except Exception:
                    pass
        t = asyncio.create_task(go())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)


class WorkerStream:
    """One WebSocket per worker with a deterministic clientId (D-13)."""

    def __init__(self, hub: "EventHub", worker_id: int, base_url: str, name: str | None = None):
        self.hub, self.worker_id, self.base_url = hub, worker_id, base_url
        self.who = f"{name} (#{worker_id})" if name else str(worker_id)
        self.ever_connected = False                 # before the first connection its failures are the monitor's news
        self.client_id = worker_client_id(hub.s.instance, worker_id)
        # The job the worker is executing, as this connection saw it start: the only owner of frames that carry no
        # prompt_id (type-1 previews, older events). None = not positively known → such frames are dropped (D-28).
        self.current: JobRef | None = None
        self.last_progress_write = 0.0
        self.last_written = None
        self.track: JobTrack | None = None          # the job the progress numbers below belong to
        self.last_state = None
        self.connected = False
        self.down_since = time.monotonic()
        self.last_error: str | None = None          # logged once per streak, not on every retry

    async def run(self):
        backoff = [1, 2, 5, 10]
        attempt = 0
        url = self.base_url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}"
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT) as ws:
                    # whatever ran before the gap may have ended, or the worker restarted and runs another user's job
                    self.current = None
                    self.connected, attempt = True, 0
                    if not self.ever_connected:
                        log.info("worker %s ws: event stream connected", self.who)
                    elif self.last_error:
                        log.info("worker %s ws: connected again", self.who)
                    self.ever_connected, self.last_error = True, None
                    await ws.send(json.dumps({"type": "feature_flags", "data": {"supports_preview_metadata": True}}))
                    async for msg in ws:
                        await self.on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if type(e).__name__ != self.last_error and self.ever_connected:
                    log.info("worker %s ws: %s", self.who, type(e).__name__)
                elif type(e).__name__ != self.last_error:              # not up yet: the health monitor says so
                    log.debug("worker %s ws: %s", self.who, type(e).__name__)
                self.last_error = type(e).__name__
            if self.connected:
                self.down_since = time.monotonic()
            self.connected, self.current = False, None
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
            attempt += 1

    def tracked(self, ref: JobRef) -> JobTrack:
        if self.track is None or self.track.job_id != ref.job_id:
            self.track, self.last_state = JobTrack(ref.job_id), None
        return self.track

    async def on_message(self, msg):
        if isinstance(msg, (bytes, bytearray)):                 # live preview frame, or a node's progress text
            text = parse_text(bytes(msg))
            if text and self.current is not None and (m := ITERATION.match(text[1].strip())):
                await self.iteration(self.current, text[0], int(m.group(1)), int(m.group(2)))
            meta, frame = parse_preview(bytes(msg))
            ref = await self.hub.ref(meta["prompt_id"]) if meta and meta.get("prompt_id") else self.current
            if ref is not None:
                self.hub.browsers.send_preview(self.hub.browsers.target(ref), frame, meta)
            return
        try:
            ev = json.loads(msg)
        except ValueError:
            return
        etype, data = ev.get("type"), ev.get("data") or {}
        pid = data.get("prompt_id")
        ref = await self.hub.ref(pid) if pid else self.current
        if etype == "execution_start" and ref is not None:
            self.current = ref
            await self.hub.started(ref)
        elif etype == "execution_cached" and ref is not None:   # cached nodes did not run, but they would have
            self.tracked(ref).ran.update(prompt_node(str(k)) for k in data.get("nodes") or [])
        elif etype == "executing" and pid and ref is not None:
            if data.get("node") is not None:
                self.current = ref          # a node of this prompt runs now (re-learns the job after a reconnect)
            elif self.current is not None and self.current.prompt_id == ref.prompt_id:
                self.current = None         # node None: this prompt is done
        if etype == "executed" and ref is not None and data.get("node") is not None:
            # record before relaying: the browser asks /api/view for these files as soon as it sees the event, and
            # the gateway finds the worker holding them through the ledger
            await self.hub.record_output(ref, str(data["node"]), data.get("output") or {})
        if ref is not None:
            self.hub.browsers.send(self.hub.browsers.target(ref), ev)      # the target is captured now (D-16)
        if ref is None:
            return
        if etype in ("progress", "progress_state"):
            await self.progress(ref, data)
        elif etype in TERMINAL_EVENTS:
            if etype == "execution_success" and self.track is not None and self.track.job_id == ref.job_id:
                await self.hub.remember_run(ref, self.track.ran)
            await self.hub.finish(ref, TERMINAL_EVENTS[etype], data)
            if self.current is not None and self.current.prompt_id == ref.prompt_id:
                self.current = None

    async def progress(self, ref: JobRef, data: dict):
        """At most one write per worker every 2 s, only when something changed (D-15). A dropped write costs a stale
        number for one period; a failed write never breaks the receive loop. `at` is when the numbers last moved —
        ComfyUI says nothing between two sampler steps, so a long step is a long silence, not a dead worker."""
        tr, now = self.tracked(ref), time.time()
        value = data.get("nodes") if "nodes" in data else {"value": data.get("value"), "max": data.get("max"), "node": data.get("node")}
        if value != self.last_state:
            self.last_state, tr.state_at = value, now
        nodes = value if "nodes" in data else {str(data.get("node")): value}
        for nid, n in (nodes or {}).items():
            if not isinstance(n, dict):
                continue
            tr.ran.add(prompt_node(str(nid)))
            v, mx = n.get("value") or 0, n.get("max") or 0
            if mx > 1 and v >= 1 and nid not in tr.steps:
                tr.steps[str(nid)] = [now, v]
        await self.write_progress(tr)

    async def iteration(self, ref: JobRef, node_id: str, k: int, n: int):
        """"Iteration k / N" of a loop: 0 when it starts, k when its k-th iteration ended."""
        tr = self.tracked(ref)
        loop = tr.loops.setdefault(node_id, {"n": n, "ends": []})
        loop["n"] = n
        if k == len(loop["ends"]):
            loop["ends"].append(time.time())
        await self.write_progress(tr, force=True)

    async def write_progress(self, tr: JobTrack, force: bool = False):
        now = time.monotonic()
        doc = tr.doc(self.last_state)
        if doc == self.last_written or (now - self.last_progress_write < PROGRESS_THROTTLE and not force):
            return
        self.last_progress_write, self.last_written = now, json.loads(json.dumps(doc))
        try:
            async with self.hub.db.conn() as c:
                await c.execute("UPDATE jobs SET progress = $2 WHERE id = $1 AND status IN ('running','dispatching','cancelling')",
                                tr.job_id, doc)
        except Exception as e:
            log.debug("progress write skipped: %s", type(e).__name__)


class EventHub:
    def __init__(self, db: Database, settings: Settings, wake: asyncio.Event | None = None):
        self.db, self.s, self.wake = db, settings, wake
        self.browsers = BrowserHub()
        self.streams: dict[int, tuple[WorkerStream, asyncio.Task]] = {}
        self.refs: dict[str, JobRef] = {}
        self.status_seen: dict[str, tuple] = {}
        self.running = False

    STREAM_GRACE = 30.0

    def ready(self, worker_id: int) -> bool:
        """Dispatch to a worker only when its event stream is up: events of a job submitted while nobody listens on
        the router's clientId are lost (P10). If the stream stays down longer than the grace, dispatch anyway —
        outcomes still come from reconcile, only live progress is lost."""
        if not self.running:
            return True
        entry = self.streams.get(worker_id)
        if entry is None:
            return False
        st = entry[0]
        return st.connected or time.monotonic() - st.down_since > self.STREAM_GRACE

    async def ref(self, prompt_id: str) -> JobRef | None:
        """prompt_id → job, cached; on a miss rebuilt from the ledger (D-17)."""
        r = self.refs.get(prompt_id)
        if r is not None:
            return r
        try:
            pid = uuid.UUID(prompt_id)
        except ValueError:
            return None
        async with self.db.conn() as c:
            row = await c.fetchrow("SELECT id, owner, browser_client_id FROM jobs WHERE prompt_id = $1", pid)
        if row is None:
            return None                                           # not ours (someone used the worker directly)
        r = JobRef(row["id"], prompt_id, row["owner"], row["browser_client_id"])
        if len(self.refs) > 10000:
            self.refs.clear()
        self.refs[prompt_id] = r
        return r

    async def record_output(self, ref: JobRef, node: str, output: dict):
        try:
            async with self.db.conn() as c:
                await c.execute("UPDATE jobs SET outputs = coalesce(outputs, '{}'::jsonb) || jsonb_build_object($2::text, $3::jsonb) "
                                "WHERE id = $1", ref.job_id, node, output)
        except Exception as e:
            log.warning("output record skipped: %s", type(e).__name__)

    async def started(self, ref: JobRef):
        try:
            async with self.db.conn() as c:
                await jobsvc.transition(c, ref.job_id, "running", ("dispatching",), "worker: execution_start")
                await c.execute("UPDATE jobs SET started_at = coalesce(started_at, now()) WHERE id = $1", ref.job_id)
        except Busy:
            pass

    async def remember_run(self, ref: JobRef, ran: set[str]):
        """The prompt nodes a successful run executed, under its workflow's shape: the next run of the same shape
        weighs only those (a lazy input's branch that never runs must not hold its bar back)."""
        try:
            async with self.db.conn() as c:
                prompt = await c.fetchval("SELECT prompt FROM job_payloads WHERE job_id = $1", ref.job_id)
                shape = jobsvc.workflow_shape(prompt)
                ids = sorted(ran & set(prompt or {}))
                if shape and ids:
                    await c.execute("INSERT INTO workflow_runs(shape, ran) VALUES ($1, $2) "
                                    "ON CONFLICT (shape) DO UPDATE SET ran = EXCLUDED.ran, at = now()", shape, ids)
        except Exception as e:
            log.debug("workflow run not remembered: %s", type(e).__name__)

    async def finish(self, ref: JobRef, to: str, data: dict):
        """WS terminal events finish a job immediately; reconcile confirms against /api/jobs (D-08)."""
        fields = {}
        if to == "failed":
            fields["error"] = jobsvc.error_doc("execution", data.get("exception_message", "execution error"),
                                               node_id=data.get("node_id"), node_type=data.get("node_type"),
                                               exception_type=data.get("exception_type"))
        try:
            async with self.db.conn() as c:
                async with c.transaction():
                    await jobsvc.transition(c, ref.job_id, to, ("dispatching", "running", "cancelling"), f"ws: {to}", **fields)
        except Busy:
            return                                                 # reconcile will get it
        if self.wake:
            self.wake.set()

    # ---- workers -------------------------------------------------------------------------------------------------
    async def sync_workers(self):
        """Keep one stream per enabled worker; called periodically."""
        async with self.db.conn() as c:
            rows = await c.fetch("SELECT id, name, base_url FROM workers WHERE enabled AND deleted_at IS NULL")
        want = {r["id"]: r["base_url"] for r in rows}
        names = {r["id"]: r["name"] for r in rows}
        for wid in list(self.streams):
            if wid not in want or self.streams[wid][0].base_url != want[wid]:
                self.streams.pop(wid)[1].cancel()
        for wid, url in want.items():
            if wid not in self.streams:
                st = WorkerStream(self, wid, url, names[wid])
                self.streams[wid] = (st, asyncio.create_task(st.run()))

    async def run(self):
        self.running = True
        while True:
            try:
                await self.sync_workers()
                await self.push_status()
            except Busy:
                pass
            except Exception:
                log.exception("event hub tick failed")
            await asyncio.sleep(1.0)

    async def stop(self):
        for _, t in self.streams.values():
            t.cancel()

    # ---- browsers ------------------------------------------------------------------------------------------------
    async def push_status(self, owners: set[str] | None = None):
        """Queue `status` for each connected owner when it changes (D-18) — the stock frontend refreshes its queue
        panel only on this event."""
        owners = owners or {c.owner for c in self.browsers.conns.values()}
        if not owners:
            return
        async with self.db.conn() as c:
            rows = await c.fetch("SELECT owner, count(*) FILTER (WHERE status IN ('pending','paused')) AS pending, "
                                 "count(*) FILTER (WHERE status IN ('dispatching','running','cancelling')) AS running "
                                 "FROM jobs WHERE owner = ANY($1) AND deleted_at IS NULL GROUP BY owner", list(owners))
        counts = {r["owner"]: (r["pending"], r["running"]) for r in rows}
        for owner in owners:
            cur = counts.get(owner, (0, 0))
            if self.status_seen.get(owner) == cur:
                continue
            self.status_seen[owner] = cur
            msg = {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": cur[0] + cur[1]}}}}
            for conn in [c for c in self.browsers.conns.values() if c.owner == owner]:
                self.browsers.send(conn, msg)

    async def catch_up(self, conn: BrowserConn):
        """Final events this browser may have missed while it was away (D-19)."""
        async with self.db.conn() as c:
            rows = await c.fetch(
                "SELECT prompt_id, status, error FROM jobs WHERE owner = $1 AND browser_client_id = $2 "
                "AND status IN ('completed','failed','cancelled') AND finished_at > now() - make_interval(secs => $3)",
                conn.owner, conn.client_id, CATCH_UP_WINDOW)
        for r in rows:
            pid = str(r["prompt_id"])
            if r["status"] == "completed":
                self.browsers.send(conn, {"type": "execution_success", "data": {"prompt_id": pid, "catch_up": True}})
            elif r["status"] == "failed":
                err = r["error"] or {}
                self.browsers.send(conn, {"type": "execution_error", "data": {"prompt_id": pid, "catch_up": True,
                                                                               "exception_message": err.get("message", "")}})
            else:
                self.browsers.send(conn, {"type": "execution_interrupted", "data": {"prompt_id": pid, "catch_up": True}})
            self.browsers.send(conn, {"type": "executing", "data": {"node": None, "prompt_id": pid}})
