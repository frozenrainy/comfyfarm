"""Submit API graphs to a worker one after another; report wall time, sampler s/it and peak RAM/VRAM per run.
python spikes/run_and_time.py http://host:8188 run1.json run2.json ..."""
import json, re, sys, threading, time, uuid
import httpx

base, files = sys.argv[1], sys.argv[2:]
c = httpx.Client(timeout=30)


def poll(stop, peaks):
    while not stop.is_set():
        try:
            s = c.get(f"{base}/system_stats").json()
            peaks["ram_used"] = max(peaks.get("ram_used", 0), (s["system"]["ram_total"] - s["system"]["ram_free"]) / 2**30)
            d = s["devices"][0]
            peaks["vram_used"] = max(peaks.get("vram_used", 0), (d["vram_total"] - d["vram_free"]) / 2**30)
        except Exception:
            pass
        time.sleep(1)


for f in files:
    log_from = len(c.get(f"{base}/internal/logs/raw").json()["entries"])
    pid = str(uuid.uuid4()); peaks = {}; stop = threading.Event()
    threading.Thread(target=poll, args=(stop, peaks), daemon=True).start()
    t0 = time.time()
    r = c.post(f"{base}/prompt", json={"prompt": json.load(open(f)), "prompt_id": pid, "client_id": "timer"})
    if r.status_code != 200:
        print(f, "rejected", r.text[:500]); continue
    while True:
        j = c.get(f"{base}/api/jobs/{pid}").json()
        if j.get("status") in ("completed", "failed", "cancelled"):
            break
        time.sleep(3)
    wall = time.time() - t0; stop.set()
    entries = c.get(f"{base}/internal/logs/raw").json()["entries"][log_from:]
    text = "".join(e["m"] for e in entries)
    its = re.findall(r"(\d+)/(\d+) \[([\d:]+)<[^,]*,\s*([\d.]+)(s/it|it/s)", text)
    steps = {}
    for done, total, _, v, unit in its:
        if done == total:
            steps.setdefault(total, []).append(float(v) if unit == "s/it" else 1 / float(v))
    err = j.get("execution_error")
    print(f"{f}: {j['status']} wall {wall:.0f}s | peak RAM {peaks.get('ram_used', 0):.1f} GB, VRAM {peaks.get('vram_used', 0):.1f} GB | "
          f"s/it by loop length {{{', '.join(f'{k}: {[round(x, 1) for x in v]}' for k, v in steps.items())}}}" + (f" | error {err}" if err else ""))
    sys.stdout.flush()
