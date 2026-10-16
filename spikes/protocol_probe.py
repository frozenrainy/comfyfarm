"""ComfyUI worker protocol probe — verifies the assumptions the router design relies on.
Usage: python spikes/protocol_probe.py http://127.0.0.1:18188
Each check prints PASS/FAIL/INFO with the observed evidence; nothing here mutates the worker beyond submitting tiny jobs."""
import asyncio, json, sys, time, uuid
import httpx, websockets

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:18188"
WS = BASE.replace("http", "ws", 1) + "/ws"

def tiny(seed=0, size=64, blur=1):
    return {"1": {"class_type": "EmptyImage", "inputs": {"width": size, "height": size, "batch_size": 1, "color": seed % 0xFFFFFF}},
            "2": {"class_type": "ImageBlur", "inputs": {"image": ["1", 0], "blur_radius": blur, "sigma": 1.0}},
            "3": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}}}

def slow(seed=0):  # one long single node (tens of seconds on CPU) — interrupt cannot cut it (P7)
    return tiny(seed, size=2048, blur=25)

def report(tag, ok, detail):
    print(f"[{'PASS' if ok is True else 'FAIL' if ok is False else 'INFO'}] {tag}: {detail}")

async def collect(ws, seconds):
    out = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            break
        out.append(json.loads(m)["type"] if isinstance(m, str) else f"binary:{len(m)}")
    return out

async def wait_history(c, pid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = (await c.get(f"{BASE}/history/{pid}")).json()
        if pid in h:
            return h[pid], time.time() - t0
        await asyncio.sleep(0.2)
    return None, timeout

async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        q = (await c.get(f"{BASE}/queue")).json()
        if q["queue_running"] or q["queue_pending"]:   # results against a busy worker are meaningless (shared-worker trap)
            print(f"[ABORT] worker queue not empty (running {len(q['queue_running'])}, pending {len(q['queue_pending'])}) — wait or clear it")
            return
        ver = (await c.get(f"{BASE}/system_stats")).json()["system"]["comfyui_version"]
        report("version", None, ver)

        # S1 client-chosen prompt_id is honoured
        pid = str(uuid.uuid4())
        r = await c.post(f"{BASE}/prompt", json={"prompt": tiny(1), "client_id": "probe", "prompt_id": pid})
        body = r.json()
        report("S1 prompt_id preassigned", body.get("prompt_id") == pid, f"status {r.status_code} sent {pid[:8]} got {str(body.get('prompt_id'))[:8]} keys {sorted(body)}")
        h, dt = await wait_history(c, pid)
        report("S1b history by preassigned id", h is not None, f"found after {dt:.1f}s, status={h and h.get('status', {}).get('status_str')}")

        # S1c uppercase id — is it normalised?
        up = str(uuid.uuid4()).upper()
        r = await c.post(f"{BASE}/prompt", json={"prompt": tiny(2), "client_id": "probe", "prompt_id": up})
        report("S1c uppercase prompt_id", None, f"status {r.status_code} -> {r.json().get('prompt_id')} {r.json().get('error', '')}")

        # S1d duplicate prompt_id — what happens on resubmission (router retry after ReadTimeout)?
        r = await c.post(f"{BASE}/prompt", json={"prompt": tiny(1), "client_id": "probe", "prompt_id": pid})
        report("S1d resubmit same prompt_id", None, f"status {r.status_code} body {json.dumps(r.json())[:200]}")
        await asyncio.sleep(2)
        hist = (await c.get(f"{BASE}/history")).json()
        report("S1d history entries for that id", None, f"{sum(1 for k in hist if k == pid)} (dict keyed by id — a rerun overwrites)")

        # S2 shapes: /queue, /api/jobs
        q = (await c.get(f"{BASE}/queue")).json()
        report("S2 /queue keys", None, sorted(q))
        r = await c.get(f"{BASE}/api/jobs")
        report("S2 /api/jobs", r.status_code == 200, f"status {r.status_code} {str(r.text)[:240]}")
        r = await c.get(f"{BASE}/api/jobs/{pid}")
        report("S2 /api/jobs/{id}", r.status_code == 200, f"status {r.status_code} {str(r.text)[:300]}")

        # S4 validation failure -> 400 + node_errors
        bad = tiny(3); bad["2"]["inputs"]["image"] = ["99", 0]
        r = await c.post(f"{BASE}/prompt", json={"prompt": bad, "client_id": "probe", "prompt_id": str(uuid.uuid4())})
        report("S4 invalid workflow -> 4xx", 400 <= r.status_code < 500, f"status {r.status_code} keys {sorted(r.json())} error.type={r.json().get('error', {}).get('type')}")
        bad2 = tiny(4); bad2["2"]["inputs"]["blur_radius"] = "x"
        r = await c.post(f"{BASE}/prompt", json={"prompt": bad2, "client_id": "probe", "prompt_id": str(uuid.uuid4())})
        ne = r.json().get("node_errors", {})
        report("S4b bad input value -> node_errors", r.status_code == 400 and "2" in ne, f"status {r.status_code} node_errors keys {list(ne)}")

        # S5 object_info cost
        t = time.time(); r = await c.get(f"{BASE}/object_info"); dt = time.time() - t
        report("S5 /object_info", None, f"{dt:.2f}s, {len(r.content)/1e6:.1f} MB, {len(r.json())} nodes")

        # S3 WS: same clientId reconnect race
        cid = "probe-" + uuid.uuid4().hex[:8]
        ws1 = await websockets.connect(f"{WS}?clientId={cid}", max_size=None)
        await collect(ws1, 0.5)
        ws2 = await websockets.connect(f"{WS}?clientId={cid}", max_size=None)
        await collect(ws2, 0.5)
        await ws1.close(); await asyncio.sleep(0.5)          # old handler's finally runs now
        p3 = str(uuid.uuid4())
        await c.post(f"{BASE}/prompt", json={"prompt": tiny(5), "client_id": cid, "prompt_id": p3})
        got = await collect(ws2, 4)
        report("S3 reconnect race (ws2 after ws1 closed)", None,
               f"ws2 received {len([g for g in got if g != 'status'])} job events {got[:8]} — 0 = bug present (registration deleted by old handler)")
        await ws2.close()

        # S3b control: single connection receives job events
        cid2 = "probe-" + uuid.uuid4().hex[:8]
        ws = await websockets.connect(f"{WS}?clientId={cid2}", max_size=None)
        await collect(ws, 0.5)
        other = await websockets.connect(f"{WS}?clientId=bystander-{uuid.uuid4().hex[:6]}", max_size=None)
        await collect(other, 0.5)
        await c.post(f"{BASE}/prompt", json={"prompt": tiny(6), "client_id": cid2, "prompt_id": str(uuid.uuid4())})
        got, got_other = await asyncio.gather(collect(ws, 4), collect(other, 4))
        report("S3b control: owner socket gets job events", len([g for g in got if g != 'status']) > 0, f"{got[:10]}")
        report("S3c bystander socket", None, f"{got_other[:10]} (only 'status' broadcasts expected)")
        await ws.close(); await other.close()

        # S7 feature flags handshake
        ws = await websockets.connect(f"{WS}?clientId=ff-{uuid.uuid4().hex[:6]}", max_size=None)
        await collect(ws, 0.3)
        await ws.send(json.dumps({"type": "feature_flags", "data": {"supports_preview_metadata": True}}))
        m = json.loads(await asyncio.wait_for(ws.recv(), 3))
        report("S7 feature_flags reply", m.get("type") == "feature_flags", json.dumps(m)[:300])
        await ws.close()

        # S6 interrupt targeted at a prompt_id, and deleting a pending item
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await c.post(f"{BASE}/prompt", json={"prompt": slow(7), "client_id": "probe", "prompt_id": a})
        await c.post(f"{BASE}/prompt", json={"prompt": slow(8), "client_id": "probe", "prompt_id": b})
        await asyncio.sleep(1.0)
        q = (await c.get(f"{BASE}/queue")).json()
        running = [x[1] for x in q["queue_running"]]; pending = [x[1] for x in q["queue_pending"]]
        report("S6 queue state", None, f"running {[r[:8] for r in running]} pending {[p[:8] for p in pending]}")
        r = await c.post(f"{BASE}/queue", json={"delete": [b]})
        q = (await c.get(f"{BASE}/queue")).json()
        report("S6b delete pending by id", b not in [x[1] for x in q["queue_pending"]], f"status {r.status_code}")
        r = await c.post(f"{BASE}/interrupt", json={"prompt_id": str(uuid.uuid4())})
        await asyncio.sleep(0.5)
        q = (await c.get(f"{BASE}/queue")).json()
        report("S6c interrupt with a NON-running prompt_id leaves the running job alone", a in [x[1] for x in q["queue_running"]], f"status {r.status_code}")
        r = await c.post(f"{BASE}/interrupt", json={"prompt_id": a})
        # interrupt is cooperative (checked between nodes): a single long node keeps running — wait for the outcome
        h, dt = await wait_history(c, a, 900)
        st = h and h.get("status", {})
        job = (await c.get(f"{BASE}/api/jobs/{a}")).json()
        msgs = [m[0] for m in (st or {}).get("messages", [])]
        report("S6d interrupt targeted at running id", h is not None and "execution_interrupted" in msgs and job.get("status") == "cancelled",
               f"took effect after {dt:.0f}s · /api/jobs status={job.get('status')} · history status_str={st and st.get('status_str')} completed={st and st.get('completed')} msgs={msgs}")
        hb = (await c.get(f"{BASE}/history/{b}")).json()
        report("S6e deleted pending job leaves no history", b not in hb, f"{list(hb)[:1]}")

        # S8 /api/jobs/{id}/cancel: idempotent, pending and unknown ids
        p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
        await c.post(f"{BASE}/prompt", json={"prompt": slow(9), "client_id": "probe", "prompt_id": p1})
        await c.post(f"{BASE}/prompt", json={"prompt": tiny(10), "client_id": "probe", "prompt_id": p2})
        await asyncio.sleep(0.5)
        r = await c.post(f"{BASE}/api/jobs/{p2}/cancel")
        report("S8 cancel API on a pending job", r.status_code == 200 and r.json().get("cancelled") is True, f"status {r.status_code} {r.text[:80]}")
        r = await c.post(f"{BASE}/api/jobs/{str(uuid.uuid4())}/cancel")
        report("S8b cancel API on an unknown id is a no-op", r.status_code == 200 and r.json().get("cancelled") is False, f"status {r.status_code} {r.text[:80]}")
        r = await c.post(f"{BASE}/api/jobs/{p1}/cancel")
        h, dt = await wait_history(c, p1, 900)
        job = (await c.get(f"{BASE}/api/jobs/{p1}")).json()
        report("S8c cancel API on the running job", r.json().get("cancelled") is True and job.get("status") == "cancelled",
               f"took effect after {dt:.0f}s, status={job.get('status')}")

asyncio.run(main())
