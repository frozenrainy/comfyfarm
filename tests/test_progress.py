"""The progress a person sees is weighed by what takes the time (field, dev.21: a 20-node video workflow sat at 5 %
through its one KSampler; dev.22: a 6-iteration loop sat at 3 % and its row said "no word" through every 3-minute
step). The router guesses a plan from the API prompt and keeps what the worker says besides the numbers; the
browser does the weighing (tests/test_browser_code.py)."""
import json
import struct
import time
import uuid

import httpx

from comfyfarm.app import create_app
from comfyfarm.core.jobs import job_loops, job_plan, workflow_shape
from comfyfarm.events import hub as hubmod
from comfyfarm.events.hub import JobRef, WorkerStream, parse_text, prompt_node
from conftest import fast_settings, tiny


def video_workflow():
    return {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
            "3": {"class_type": "KSampler", "inputs": {"steps": 10, "cfg": 5}},
            "4": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 2}},
            "5": {"class_type": "SamplerCustomAdvanced", "inputs": {}},           # no steps input: the usual count
            "6": {"class_type": "VAEDecode", "inputs": {}},
            "7": {"class_type": "VAEEncodeTiled", "inputs": {}},
            "8": {"class_type": "VAELoader", "inputs": {}},                        # a VAE, not a decode: nothing
            "9": {"class_type": "SaveImage", "inputs": {}},
            "10": {"class_type": "KSampler", "inputs": {"steps": True}},           # not a number: the usual count
            # two-pass video: the high-noise pass runs steps 0–10 of 20, the low one 10–20
            "11": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 20, "start_at_step": 0, "end_at_step": 10}},
            "12": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 20, "start_at_step": 10, "end_at_step": 10000}}}


def loop_workflow(count=6):
    """Load → StartLoop → (sample → decode → write) → EndLoop → save; the count typed in, or computed by a node."""
    n = count if isinstance(count, int) else ["20", 0]
    return {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "20": {"class_type": "ComfyMathExpression", "inputs": {"expression": "3*2"}},
            "96": {"class_type": "StartLoop", "inputs": {"mode": "simple", "mode.num_iterations": n, "initial_iteration_value": ["1", 0]}},
            "160:1": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 20, "start_at_step": 0, "end_at_step": 10, "latent_image": ["96", 3]}},
            "160:2": {"class_type": "VAEDecode", "inputs": {"samples": ["160:1", 0]}},
            "160:3": {"class_type": "FrameSeqWrite", "inputs": {"images": ["160:2", 0], "offset": ["96", 0]}},
            "160:9": {"class_type": "EndLoop", "inputs": {"output_value": ["160:3", 0], "next_iteration_value": ["160:2", 0]}},
            "200": {"class_type": "SaveImage", "inputs": {"images": ["160:9", 0]}}}


def test_the_plan_weighs_samplers_by_their_steps():
    p = job_plan(video_workflow())
    assert p["3"] == 10 and p["4"] == 2 and p["5"] == 20 and p["10"] == 20
    assert p["11"] == 10 and p["12"] == 10                       # the steps each pass runs, not the schedule's 20
    assert p["6"] == 2 and p["7"] == 2
    assert p["1"] == p["2"] == p["8"] == p["9"] == 0.1
    assert job_plan("not a prompt") == {} and job_plan({"x": "junk"}) == {}
    samplers = ("3", "4", "5", "10", "11", "12")
    assert sum(p[k] for k in samplers) / sum(p.values()) > 0.9


def test_loops_and_their_bodies_as_comfyui_sees_them():
    loops = job_loops(loop_workflow(6))
    assert loops == {"96": {"end": "160:9", "body": ["160:1", "160:2", "160:3"], "n": 6}}
    assert job_loops(loop_workflow("computed"))["96"]["n"] is None           # another node computes it: said at run time
    w = loop_workflow(6)
    w["96"]["inputs"] = {"mode": "For", "mode.start_iteration_index": 2, "mode.max_iteration": 11, "mode.step": 3}
    assert job_loops(w)["96"]["n"] == 3                                      # 2, 5, 8
    del w["160:9"]                                                           # no end: ComfyUI refuses it; nothing here
    assert job_loops(w) == {} and job_loops(video_workflow()) == {} and job_loops(None) == {}


def test_a_workflow_keeps_its_shape_whatever_its_inputs():
    a, b = loop_workflow(6), loop_workflow(6)
    b["1"]["inputs"]["image"] = "other.png"
    b["160:1"]["inputs"]["steps"] = 30
    assert workflow_shape(a) == workflow_shape(b)
    b["5"] = {"class_type": "PreviewImage", "inputs": {}}
    assert workflow_shape(a) != workflow_shape(b) and workflow_shape({}) is None


def test_ids_of_loop_copies_and_text_frames():
    assert prompt_node("96.0.0.3_160:137") == "160:137" and prompt_node("160:137") == "160:137"
    assert prompt_node("96.0.0.0_160:200.0.0.2_160:210") == "160:210"           # nested: the innermost
    assert prompt_node("96.0.0.iteration_0") == "96.0.0.iteration_0"
    frame = struct.pack(">I", 3) + struct.pack(">I", 2) + b"96" + "Iteration 2 / 6".encode()
    assert parse_text(frame) == ("96", "Iteration 2 / 6")
    assert parse_text(struct.pack(">I", 4) + b"\0" * 8) is None and parse_text(b"\0\0") is None


class Recorder:
    """A hub whose ledger is a list: what the stream would have written."""

    def __init__(self):
        self.writes = []
        hub = self

        class Conn:
            async def execute(self, q, *a):
                hub.writes.append(a)

        class Ctx:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *e):
                return False

        self.db = type("Db", (), {"conn": lambda self: Ctx()})()
        self.s = type("S", (), {"instance": "test"})()
        self.relayed = []
        self.browsers = type("B", (), {"target": lambda _, ref: ref.owner,
                                       "send_preview": lambda _, to, frame, meta: hub.relayed.append(to),
                                       "send": lambda _, to, ev: hub.relayed.append(to)})()

        self.refs = {}

    async def ref(self, prompt_id):
        return self.refs.get(prompt_id)


async def test_the_stream_keeps_first_steps_iterations_and_what_ran(monkeypatch):
    rec = Recorder()
    st = WorkerStream(rec, 1, "http://192.0.2.10:8188")
    ref = JobRef(7, str(uuid.uuid4()), "ann", None)
    rec.refs[ref.prompt_id] = ref
    st.current = ref
    clock = [1000.0]
    monkeypatch.setattr(hubmod.time, "time", lambda: clock[0])
    monkeypatch.setattr(hubmod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hubmod, "PROGRESS_THROTTLE", 0)

    text = lambda node, k, n: struct.pack(">I", 3) + struct.pack(">I", len(node)) + node.encode() + f"Iteration {k} / {n}".encode()
    await st.on_message(text("96", 0, 3))                                        # the loop starts
    await st.progress(ref, {"nodes": {"1": {"value": 1, "max": 1, "state": "finished"},
                                      "96.0.0.0_160:1": {"value": 0, "max": 10, "state": "running"}}})
    clock[0] = 1100
    await st.progress(ref, {"nodes": {"96.0.0.0_160:1": {"value": 1, "max": 10, "state": "running"}}})   # step 1 after loading
    clock[0] = 1300
    await st.progress(ref, {"nodes": {"96.0.0.0_160:1": {"value": 3, "max": 10, "state": "running"}}})
    clock[0] = 1500
    await st.on_message(text("96", 1, 3))                                        # iteration 1 of 3 ended
    await st.on_message(text("96", 1, 3))                                        # said twice: counted once
    await st.on_message(struct.pack(">I", 3) + struct.pack(">I", 1) + b"5" + b"loading 40%")   # another node's text
    doc = rec.writes[-1][1]
    assert rec.writes[-1][0] == 7
    assert doc["steps"] == {"96.0.0.0_160:1": [1100, 1]}                          # pace: (1300 - 1100) / (3 - 1)
    assert doc["loops"] == {"96": {"n": 3, "ends": [1000, 1500]}}
    assert doc["at"] == 1300                                                     # the numbers last moved then, not at 1500
    assert st.track.ran == {"1", "160:1"}
    await st.on_message(json.dumps({"type": "execution_cached", "data": {"prompt_id": ref.prompt_id, "nodes": ["20"]}}))
    assert "20" in st.track.ran                                                  # cached ran before: it would again
    assert rec.relayed.count("ann") >= 4                                         # the texts still reach the browser


async def test_a_learned_plan_leaves_out_what_did_not_run(dsn):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            H = {"X-ComfyFarm-User": "ann"}
            wf = loop_workflow("computed")
            wf["30"] = {"class_type": "KSampler", "inputs": {"steps": 30}}      # a lazy branch: never runs
            a = (await c.post("/api/prompt", json={"prompt": wf}, headers=H)).json()["prompt_id"]
            b = (await c.post("/api/prompt", json={"prompt": tiny(2)}, headers=H)).json()["prompt_id"]
            async with r.db.conn() as conn:
                await conn.execute("UPDATE jobs SET status = 'running', started_at = now() WHERE prompt_id = $1", uuid.UUID(a))
                jid = await conn.fetchval("SELECT id FROM jobs WHERE prompt_id = $1", uuid.UUID(a))
            first = {j["prompt_id"]: j for j in (await c.get("/api/farm/queue", headers=H)).json()["jobs"]}
            assert first[a]["plan"]["30"] == 30 and first[a]["plan_learned"] is False
            assert first[a]["loops"]["96"]["body"] == ["160:1", "160:2", "160:3"]
            assert "plan" not in first[b]                                        # waiting: nothing to weigh yet

            # a successful run of the same shape remembers what ran — the lazy branch did not
            await r.events.remember_run(JobRef(jid, a, "ann", None), {"1", "20", "96", "160:1", "160:2", "160:3", "200", "x:junk"})
            for path in ("/api/farm/queue", "/api/farm/mine", "/api/farm/dashboard"):
                d = (await c.get(path, headers=H)).json()
                j = {j["prompt_id"]: j for j in d.get("jobs") or d.get("queue")}[a]
                assert j["plan"]["30"] == 0 and j["plan"]["160:1"] == 10 and j["plan_learned"] is True, path
            async with r.db.conn() as conn:
                assert sorted(await conn.fetchval("SELECT ran FROM workflow_runs")) == ["1", "160:1", "160:2", "160:3", "20", "200", "96"]


async def test_a_running_job_says_when_its_worker_is_not_answering(dsn):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            H = {"X-ComfyFarm-User": "ann"}
            a = (await c.post("/api/prompt", json={"prompt": tiny(1)}, headers=H)).json()["prompt_id"]
            wid = (await c.post("/api/admin/workers", json={"name": "w", "base_url": "http://192.0.2.20:8188"},
                                headers={"X-ComfyFarm-User": "admin"})).json()["id"]
            async with r.db.conn() as conn:
                await conn.execute("UPDATE jobs SET status = 'running', worker_id = $2 WHERE prompt_id = $1", uuid.UUID(a), wid)
                await conn.execute("UPDATE workers SET state = 'online' WHERE id = $1", wid)
            j = (await c.get("/api/farm/queue", headers=H)).json()["jobs"][0]
            assert "worker_state" not in j                                       # online: nothing to explain
            async with r.db.conn() as conn:
                await conn.execute("UPDATE workers SET state = 'offline' WHERE id = $1", wid)
            assert (await c.get("/api/farm/queue", headers=H)).json()["jobs"][0]["worker_state"] == "offline"
