"""What a person knows a queued job by (field, dev.17: several queued jobs looked alike): the title typed next to Run
(else the workflow's tab name, the extension's), and what the router reads from the workflow — where it saves and the
start of its prompt."""
import asyncio

import httpx

from comfyfarm.app import create_app
from comfyfarm.core.jobs import clean_title, job_hint
from conftest import add_worker, fast_settings, tiny, wait_status


def sd_workflow(positive="a red fox in the snow, golden hour", negative="blurry, low quality"):
    # the negative prompt's node comes first by id: the hint follows the sampler's `positive`, not the node order
    return {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["4", 1]}},
            "3": {"class_type": "KSampler", "inputs": {"positive": ["7", 0], "negative": ["6", 0], "seed": 1}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox/shot", "images": ["8", 0]}}}


def test_the_hint_is_where_it_saves_and_the_prompt_it_runs():
    assert job_hint(sd_workflow()) == "fox/shot · a red fox in the snow, golden hour"
    combined = sd_workflow()
    combined["10"] = {"class_type": "ConditioningCombine", "inputs": {"conditioning_1": ["7", 0], "conditioning_2": ["6", 0]}}
    combined["3"]["inputs"]["positive"] = ["10", 0]
    assert job_hint(combined).endswith("a red fox in the snow, golden hour")          # through a combiner
    wan = {"16": {"class_type": "WanVideoTextEncode", "inputs": {"positive_prompt": "a drone shot over the sea",
                                                                  "negative_prompt": "static"}},
           "27": {"class_type": "WanVideoSampler", "inputs": {"text_embeds": ["16", 0]}},
           "30": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "wan/sea", "images": ["28", 0]}}}
    assert job_hint(wan) == "wan/sea · a drone shot over the sea"                     # no `positive`: a prompt node
    long = job_hint(sd_workflow(positive="word " * 40))
    assert long.endswith("…") and len(long.split(" · ")[1]) == 60
    assert job_hint(tiny(1)) is None and job_hint({}) is None and job_hint("x") is None
    assert job_hint({"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": ["5", 0]}}}) is None   # a link, not text


def test_a_title_is_one_short_line():
    assert clean_title("  shot 12 \n take\t3\x00 ") == "shot 12 take 3"
    assert clean_title("x" * 300) == "x" * 100
    assert clean_title("   ") is None and clean_title(None) is None and clean_title(5) is None


async def test_titles_reach_my_queue_history_and_retries(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        w = await fakes()
        await add_worker(r, w.url)
        me = {"X-ComfyFarm-User": "tester"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            wf = {**tiny(1), "4": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "harbour/dawn"}}}
            pid = (await c.post("/api/prompt", json={"prompt": wf, "comfyfarm_title": "harbour\ttake 2\x07"},
                                headers=me)).json()["prompt_id"]
            await wait_status(r, pid, "completed")
            for _ in range(50):                                    # outputs are collected after completion
                mine = (await c.get("/api/farm/mine", headers=me)).json()
                if mine["recent"] and mine["recent"][0].get("preview"):
                    break
                await asyncio.sleep(0.1)
            job = mine["recent"][0]
            assert (job["title"], job["hint"]) == ("harbour take 2", "harbour/dawn")
            assert job["preview"]["filename"].startswith("fake_")                     # the editor's thumbnail
            dash = (await c.get("/api/farm/dashboard", headers=me)).json()
            assert dash["recent"][0]["title"] == "harbour take 2" and dash["recent"][0].get("preview")
            found = (await c.get("/api/farm/history", params={"q": "HARBOUR"}, headers=me)).json()["jobs"]
            assert [j["prompt_id"] for j in found] == [pid]
            again = (await c.post(f"/api/farm/jobs/{pid}/retry", headers=me)).json()["prompt_id"]
            await wait_status(r, again, "completed")
            assert {j["title"] for j in (await c.get("/api/farm/mine", headers=me)).json()["recent"]} == {"harbour take 2"}
            plain = (await c.post("/api/prompt", json={"prompt": tiny(2)}, headers=me)).json()["prompt_id"]   # neither
            await wait_status(r, plain, "completed")
            got = next(j for j in (await c.get("/api/farm/mine", headers=me)).json()["recent"] if j["prompt_id"] == plain)
            assert got["title"] is None and got["hint"] is None
            other = (await c.get("/api/farm/mine", headers={"X-ComfyFarm-User": "mallory"})).json()
            assert other == {"queue": [], "recent": []}                              # one's own jobs only
