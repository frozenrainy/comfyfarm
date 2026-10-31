"""Memory of the host and of this ComfyUI, for GET /comfyfarm/health: what the dashboard shows and /metrics exports.
Cheap enough for every health check (psutil, a few small /proc and sysfs reads).

- RAM: `available`, not `free` — page cache is free for the taking, and "free" makes a busy host look full.
- Swap: used, and the bytes swapped in/out since boot (Linux; the router turns two readings into a rate). Swapping
  in is what makes a job crawl (field: 150 MB/s for minutes against 0 when it fits) — "used" alone stays high long
  after the pressure is gone.
- RSS of this process: the models ComfyUI holds in RAM.
- VRAM per device: what the device has in use and what this process holds.

The health check runs on the server's thread while the executor may be loading a model on the GPU. On ROCm the
reading therefore makes no GPU runtime call at all (0.12): the devices (name, size, PCI address) are read once when
the extension loads, before any job; the device's use comes from the driver (sysfs mem_info_vram_used) and this
process's from its DRM file descriptors (/proc/self/fdinfo, drm-memory-vram). A health check has no business calling
the GPU runtime while a job runs (GPU faults that began alongside 0.11 turned out to have another cause, D-52). torch.cuda.mem_get_info
(what ComfyUI's /system_stats reports) is not used there either: it said 14.1 GiB in use where the driver said 23.5.
On CUDA the device's use is mem_get_info (the driver's own figure) and this process's what torch reserved.
COMFYFARM_MEMORY=0 turns the reading off.
"""
from __future__ import annotations

import glob
import os
import sys

SYSFS_PCI = "/sys/bus/pci/devices"
DEVICES: list[dict] | None = None          # read once by prime(): [{index, name, total, pci}]


def enabled() -> bool:
    return os.environ.get("COMFYFARM_MEMORY", "1") != "0"


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


def _rocm(torch) -> bool:
    return bool(getattr(getattr(torch, "version", None), "hip", None))


def prime(torch=None) -> list[dict]:
    """The devices as torch numbers them, read once (at extension load: ComfyUI has set up the GPU, no job runs)."""
    global DEVICES
    if torch is None:
        try:
            import torch
        except ImportError:
            DEVICES = []
            return DEVICES
    try:
        if not torch.cuda.is_available():
            DEVICES = []
            return DEVICES
        out = []
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            out.append({"index": i, "name": p.name, "total": int(p.total_memory), "pci": _pci(p), "rocm": _rocm(torch)})
        DEVICES = out
    except Exception:
        DEVICES = []
    return DEVICES


def process_vram(fdinfo: str = "/proc/self/fdinfo") -> dict[str, int]:
    """PCI address → bytes of VRAM this process holds, from its DRM clients (amdgpu: drm-memory-vram in KiB)."""
    seen, out = set(), {}
    for f in glob.glob(os.path.join(fdinfo, "*")):
        try:
            with open(f) as fh:
                kv = dict(line.split(":", 1) for line in fh if ":" in line)
        except OSError:
            continue
        pdev, client, vram = kv.get("drm-pdev", "").strip(), kv.get("drm-client-id", "").strip(), kv.get("drm-memory-vram", "")
        if not pdev or not vram or (pdev, client) in seen:     # one client can sit behind several descriptors
            continue
        seen.add((pdev, client))
        parts = vram.split()
        try:
            n = int(parts[0]) * {"KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}.get(parts[1] if len(parts) > 1 else "", 1)
        except (ValueError, IndexError):
            continue
        out[pdev] = out.get(pdev, 0) + n
    return out


def devices(torch=None, sysfs: str = SYSFS_PCI, fdinfo: str = "/proc/self/fdinfo") -> list[dict]:
    devs = DEVICES if DEVICES is not None else prime(torch)
    if not devs:
        return []
    out = []
    mine = process_vram(fdinfo) if any(d["rocm"] for d in devs) else {}
    for d in devs:
        r = {"index": d["index"], "name": d["name"], "total": d["total"], "used": None, "reserved": None, "used_from": None}
        if d["rocm"]:                                         # no GPU runtime call: sysfs and /proc only
            if d["pci"]:
                used = _read_int(os.path.join(sysfs, d["pci"], "mem_info_vram_used"))
                total = _read_int(os.path.join(sysfs, d["pci"], "mem_info_vram_total"))
                if used is not None:
                    r.update(used=used, used_from="driver", total=total or d["total"])
                r["reserved"] = mine.get(d["pci"])
        else:
            if torch is None:
                import torch
            try:
                free, total = torch.cuda.mem_get_info(d["index"])     # CUDA: the driver's own figure
                r.update(used=int(total - free), used_from="driver", total=int(total),
                         reserved=int(torch.cuda.memory_reserved(d["index"])))
            except Exception as e:                            # a broken GPU context: the device check reports it
                r = {"error": f"{type(e).__name__}: {e}"[:200]}
        out.append(r)
    return out


def sample(psutil=None, torch=None, sysfs: str = SYSFS_PCI, fdinfo: str = "/proc/self/fdinfo") -> dict:
    if not enabled():
        return {"off": True}
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
        "devices": devices(torch, sysfs, fdinfo),
    }
