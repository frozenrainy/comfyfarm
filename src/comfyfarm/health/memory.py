"""Host and GPU memory of each worker, as the last health check saw it — for the dashboard and /metrics.

Kept in the router's memory, not the ledger: a reading every few seconds per worker is not history worth a write, and
/metrics is where history belongs (Prometheus keeps it). A reading older than STALE is not shown: an old number next
to a worker that stopped answering would read as current.

From extension 0.11 the worker reports it on GET /comfyfarm/health (worker_ext/comfyfarm_worker/memory.py). An older
worker is read from what /system_stats has: RAM, and the VRAM ComfyUI itself reserved — not the device's use, which
ComfyUI takes from torch.cuda.mem_get_info and which undercounts on ROCm by gigabytes (field, 10-29).
"""
from __future__ import annotations

import time

STALE = 30.0           # seconds after which a reading is not shown


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def from_extension(m) -> dict | None:
    if not isinstance(m, dict) or m.get("error") or _int(m.get("ram_total")) is None:
        return None
    devs = []
    for d in m.get("devices") or []:
        if isinstance(d, dict) and not d.get("error"):
            devs.append({"index": _int(d.get("index")), "name": str(d.get("name") or "")[:100], "total": _int(d.get("total")),
                         "used": _int(d.get("used")), "reserved": _int(d.get("reserved"))})
    return {"from": "extension", "ram_total": _int(m.get("ram_total")), "ram_available": _int(m.get("ram_available")),
            "swap_total": _int(m.get("swap_total")), "swap_used": _int(m.get("swap_used")),
            "swap_in": _int(m.get("swap_in")), "swap_out": _int(m.get("swap_out")), "rss": _int(m.get("rss")),
            "devices": devs}


def from_system_stats(j) -> dict | None:
    s = (j or {}).get("system") if isinstance(j, dict) else None
    if not isinstance(s, dict) or _int(s.get("ram_total")) is None:
        return None
    devs = []
    for i, d in enumerate(j.get("devices") or []):
        if isinstance(d, dict) and d.get("type") != "cpu":
            # torch_vram_total is what torch reserved; vram_free is mem_get_info's — not used (see above)
            devs.append({"index": _int(d.get("index")) if d.get("index") is not None else i, "name": str(d.get("name") or "")[:100],
                         "total": _int(d.get("vram_total")), "used": None, "reserved": _int(d.get("torch_vram_total"))})
    return {"from": "system_stats", "ram_total": _int(s.get("ram_total")), "ram_available": _int(s.get("ram_free")),
            "swap_total": None, "swap_used": None, "swap_in": None, "swap_out": None, "rss": None, "devices": devs}


class MemoryBoard:
    """worker id → its last reading, with swap rates from the two last readings."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.readings: dict[int, tuple[float, dict]] = {}
        self.rates: dict[int, dict] = {}
        self.names: dict[int, str] = {}

    def update(self, worker_id: int, m: dict | None, name: str | None = None):
        if m is None:
            return
        if name:
            self.names[worker_id] = name
        now = self.clock()
        prev = self.readings.get(worker_id)
        rates = {"swap_in_rate": None, "swap_out_rate": None}
        if prev is not None and 0 < now - prev[0] <= STALE:
            dt = now - prev[0]
            for k in ("swap_in", "swap_out"):
                a, b = prev[1].get(k), m.get(k)
                if a is not None and b is not None and b >= a:          # a reboot resets the counters: no rate
                    rates[k + "_rate"] = (b - a) / dt
        self.readings[worker_id] = (now, m)
        self.rates[worker_id] = rates

    def view(self, worker_id: int) -> dict | None:
        r = self.readings.get(worker_id)
        if r is None or self.clock() - r[0] > STALE:
            return None
        age = self.clock() - r[0]
        return {**r[1], **self.rates.get(worker_id, {}), "age": round(age, 1)}

    def forget(self, keep: set[int]):
        for wid in set(self.readings) - keep:
            self.readings.pop(wid, None)
            self.rates.pop(wid, None)
            self.names.pop(wid, None)

    def views(self) -> list[tuple[str, dict]]:
        """(worker name, view) of every worker with a current reading, by name."""
        out = [(self.names.get(wid, f"#{wid}"), v) for wid in self.readings if (v := self.view(wid)) is not None]
        return sorted(out, key=lambda x: x[0])


GAUGES = (  # (field, metric, help) per worker; devices have their own below
    ("ram_total", "comfyfarm_worker_ram_total_bytes", "RAM of the worker's host."),
    ("ram_available", "comfyfarm_worker_ram_available_bytes", "RAM available on the worker's host (MemAvailable, not free)."),
    ("swap_total", "comfyfarm_worker_swap_total_bytes", "Swap of the worker's host."),
    ("swap_used", "comfyfarm_worker_swap_used_bytes", "Swap in use on the worker's host."),
    ("swap_in_rate", "comfyfarm_worker_swap_in_bytes_per_second", "Swapped in per second over the last health check."),
    ("swap_out_rate", "comfyfarm_worker_swap_out_bytes_per_second", "Swapped out per second over the last health check."),
    ("rss", "comfyfarm_worker_process_rss_bytes", "Resident memory of the worker's ComfyUI process."),
)
DEVICE_GAUGES = (
    ("total", "comfyfarm_worker_vram_total_bytes", "VRAM of the device."),
    ("used", "comfyfarm_worker_vram_used_bytes", "VRAM in use on the device, as its driver counts it (all processes)."),
    ("reserved", "comfyfarm_worker_vram_reserved_bytes", "VRAM the worker's ComfyUI has reserved (torch)."),
)


def _label(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(views: list[tuple[str, dict]]) -> str:
    """`views`: (worker name, view) of workers with a current reading. A value not known stays absent, never 0."""
    out = []
    for field, name, help_ in GAUGES:
        lines = [f'{name}{{worker="{_label(w)}"}} {v[field]:g}' if isinstance(v[field], float) else
                 f'{name}{{worker="{_label(w)}"}} {v[field]}' for w, v in views if v.get(field) is not None]
        if lines:
            out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"] + lines
    for field, name, help_ in DEVICE_GAUGES:
        lines = [f'{name}{{worker="{_label(w)}",device="{d.get("index")}"}} {d[field]}'
                 for w, v in views for d in v.get("devices") or [] if d.get(field) is not None]
        if lines:
            out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"] + lines
    return "\n".join(out) + ("\n" if out else "")
