"""The progress a person sees is weighed by what takes the time (field, dev.21: a 20-node video workflow sat at 5 %
through its one KSampler). The router guesses a plan from the API prompt; the browser does the weighing."""
import uuid

import httpx

from comfyfarm.app import create_app
from comfyfarm.core.jobs import job_plan
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
            "10": {"class_type": "KSampler", "inputs": {"steps": True}}}           # not a number: the usual count


def test_the_plan_weighs_samplers_by_their_steps():
    p = job_plan(video_workflow())
    assert p["3"] == 10 and p["4"] == 2 and p["5"] == 20 and p["10"] == 20
    assert p["6"] == 2 and p["7"] == 2
    assert p["1"] == p["2"] == p["8"] == p["9"] == 0.1
    assert job_plan("not a prompt") == {} and job_plan({"x": "junk"}) == {}
    # the samplers are most of the plan: the six other nodes together are less than a tenth of it
    assert sum(p[k] for k in ("3", "4", "5", "10")) / sum(p.values()) > 0.9


async def test_running_jobs_carry_their_plan(dsn):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            H = {"X-ComfyFarm-User": "ann"}
            a = (await c.post("/api/prompt", json={"prompt": video_workflow()}, headers=H)).json()["prompt_id"]
            b = (await c.post("/api/prompt", json={"prompt": tiny(2)}, headers=H)).json()["prompt_id"]
            async with r.db.conn() as conn:
                await conn.execute("UPDATE jobs SET status = 'running', started_at = now() WHERE prompt_id = $1", uuid.UUID(a))
            for path in ("/api/farm/queue", "/api/farm/mine", "/api/farm/dashboard"):
                d = (await c.get(path, headers=H)).json()
                jobs = d.get("jobs") or d.get("queue")
                by = {j["prompt_id"]: j for j in jobs}
                assert by[a]["status"] == "running" and by[a]["plan"]["3"] == 10 and by[a]["plan"]["9"] == 0.1, path
                assert "plan" not in by[b], path                     # waiting: nothing to weigh yet, nothing sent
