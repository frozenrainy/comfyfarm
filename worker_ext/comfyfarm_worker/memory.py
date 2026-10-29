"""Memory of the host and of this ComfyUI, for GET /comfyfarm/health (0.11): what the dashboard shows and /metrics
exports. Cheap enough for every health check (psutil, one sysfs read per GPU).

- RAM: `available`, not `free` — page cache is free for the taking, and "free" makes a busy host look full.
- Swap: used, and the bytes swapped in/out since boot (Linux; the router turns two readings into a rate). Swapping
  in is what makes a job crawl (field: 150 MB/s for minutes against 0 when it fits) — "used" alone stays high long
  after the pressure is gone.
- RSS of this process: the models ComfyUI holds in RAM.
- VRAM per device: what this process reserved (torch), and what the device has in use. On ROCm the device figure
  comes from the driver (sysfs mem_info_vram_used): torch.cuda.mem_get_info — what ComfyUI's /system_stats reports
  — said 14.1 GiB in use where the driver said 23.5 and torch alone had reserved 22.8 (R9700, ROCm 7.2, 10-29).
"""
from __future__ import annotations

import os
import sys

SYSFS_PCI = "/sys/bus/pci/devices"


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pci(props) -> str | None:
    try:
        return f"{props.pci_domain_id:04x}:{props.pci_bus_id:02x}:{props.pci_device_id:02x}.0"
    except AttributeError:
        return None


def devices(torch=None, sysfs: str = SYSFS_PCI) -> list[dict]:
    if torch is None:
        try:
            import torch
        except ImportError:
            return []
    try:
        if not torch.cuda.is_available():
            return []
        out = []
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            d = {"index": i, "name": props.name, "total": int(props.total_memory),
                 "reserved": int(torch.cuda.memory_reserved(i)), "used": None, "used_from": None}
            pci = _pci(props)
            if getattr(torch.version, "hip", None) and pci:
                used = _read_int(os.path.join(sysfs, pci, "mem_info_vram_used"))
                total = _read_int(os.path.join(sysfs, pci, "mem_info_vram_total"))
                if used is not None:
                    d.update(used=used, used_from="driver", total=total or d["total"])
            if d["used"] is None and not getattr(torch.version, "hip", None):
                free, total = torch.cuda.mem_get_info(i)               # CUDA: the driver's own figure
                d.update(used=int(total - free), used_from="driver", total=int(total))
            out.append(d)
        return out
    except Exception as e:                                       # a broken GPU context: the device check reports it
        return [{"error": f"{type(e).__name__}: {e}"[:200]}]


def sample(psutil=None, torch=None, sysfs: str = SYSFS_PCI) -> dict:
    if psutil is None:
        try:
            import psutil
        except ImportError:
            return {"error": "psutil is not installed"}
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    linux = sys.platform.startswith("linux")
    return {
        "ram_total": int(vm.total), "ram_available": int(vm.available),
        "swap_total": int(sw.total), "swap_used": int(sw.used),
        # cumulative since boot; Windows reports 0 for these, which would read as "no swapping"
        "swap_in": int(sw.sin) if linux else None, "swap_out": int(sw.sout) if linux else None,
        "rss": int(psutil.Process().memory_info().rss),
        "devices": devices(torch, sysfs),
    }
