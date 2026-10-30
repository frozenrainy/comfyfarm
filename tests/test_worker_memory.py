"""Worker memory: the extension's reading (host RAM, swap, ComfyUI's RSS, VRAM from the driver), the router's board
(rates, staleness), and where it shows: System → Workers, the dashboard's group cards, /metrics."""
import asyncio
import importlib.util
import pathlib
import types

import httpx
import pytest

from comfyfarm.app import create_app
from comfyfarm.health import memory as board_mod
from conftest import add_worker, fast_settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ext_memory", ROOT / "worker_ext" / "comfyfarm_worker" / "memory.py")
ext = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ext)
GiB = 2 ** 30
H = lambda user: {"X-ComfyFarm-User": user}           # noqa: E731


def fake_torch(hip: str | None, free: int, total: int, reserved: int):
    props = types.SimpleNamespace(name="GPU", total_memory=total, pci_domain_id=0, pci_bus_id=8, pci_device_id=0)
    cuda = types.SimpleNamespace(is_available=lambda: True, device_count=lambda: 1, get_device_properties=lambda i: props,
                                 memory_reserved=lambda i: reserved, mem_get_info=lambda i: (free, total))
    return types.SimpleNamespace(cuda=cuda, version=types.SimpleNamespace(hip=hip))


def test_vram_in_use_comes_from_the_driver_on_rocm(tmp_path):
    """Field (10-29, R9700): mem_get_info said 14.1 GiB in use, the driver 23.5, torch alone had reserved 22.8."""
    dev = tmp_path / "0000:08:00.0"
    dev.mkdir()
    (dev / "mem_info_vram_used").write_text(str(int(23.5 * GiB)))
    (dev / "mem_info_vram_total").write_text(str(32 * GiB))
    fd = tmp_path / "fdinfo"
    fd.mkdir()
    (fd / "8").write_text("drm-driver:\tamdgpu\ndrm-client-id:\t1066\ndrm-pdev:\t0000:08:00.0\n"
                          f"drm-memory-vram:\t{int(22.8 * GiB) // 1024} KiB\ndrm-memory-gtt: \t8244 KiB\n")
    (fd / "9").write_text((fd / "8").read_text())                      # the same client twice: counted once
    (fd / "3").write_text("pos:\t0\nflags:\t02\n")                    # not a GPU descriptor
    torch = fake_torch("7.2", free=int(17.9 * GiB), total=32 * GiB, reserved=1)
    ext.prime(torch)
    # 0.12: after the one read at load, a reading makes no GPU runtime call on ROCm (two faults at a model load
    # followed 0.11, which asked torch on every health check)
    def no_call(*a, **k):
        raise AssertionError("a GPU runtime call from the health check")
    for name in ("is_available", "device_count", "get_device_properties", "memory_reserved", "mem_get_info"):
        setattr(torch.cuda, name, no_call)
    d = ext.devices(torch, sysfs=str(tmp_path), fdinfo=str(fd))[0]
    assert d["used"] == int(23.5 * GiB) and d["used"] != 32 * GiB - int(17.9 * GiB)     # not mem_get_info's figure
    assert d["reserved"] == int(22.8 * GiB) // 1024 * 1024 and d["total"] == 32 * GiB    # this process, from the driver
    # no driver file (another platform): unknown, not mem_get_info's undercount
    assert ext.devices(torch, sysfs=str(tmp_path / "none"), fdinfo=str(fd))[0]["used"] is None
    # CUDA: mem_get_info is the driver's own figure
    ext.prime(fake_torch(None, free=4 * GiB, total=12 * GiB, reserved=7 * GiB))
    cuda = ext.devices(fake_torch(None, free=4 * GiB, total=12 * GiB, reserved=7 * GiB), sysfs=str(tmp_path))[0]
    assert cuda["used"] == 8 * GiB and cuda["total"] == 12 * GiB and cuda["reserved"] == 7 * GiB


def test_the_reading_can_be_turned_off_and_the_router_then_asks_nothing_else(monkeypatch):
    monkeypatch.setenv("COMFYFARM_MEMORY", "0")
    assert ext.sample() == {"off": True}
    assert board_mod.from_extension({"off": True}) is None


def test_a_broken_gpu_context_does_not_break_the_reading():
    def boom(i):
        raise RuntimeError("HIP error: an illegal memory access")
    torch = fake_torch(None, 0, 0, 0)
    ext.prime(torch)
    torch.cuda.mem_get_info = boom
    assert "illegal memory access" in ext.devices(torch)[0]["error"]
    assert board_mod.from_extension({"ram_total": 1, "ram_available": 1, "devices": ext.devices(torch)})["devices"] == []
    torch.cuda.get_device_properties = boom                         # at load: no devices, no failure
    assert ext.prime(torch) == []


def test_the_board_turns_counters_into_rates_and_hides_old_readings():
    now = [100.0]
    b = board_mod.MemoryBoard(clock=lambda: now[0])
    m = lambda sin: {"ram_total": 60 * GiB, "ram_available": 30 * GiB, "swap_in": sin, "swap_out": 0, "devices": []}  # noqa: E731
    b.update(1, m(1000 * 2**20), "gpu-1")
    assert b.view(1)["swap_in_rate"] is None                            # one reading: no rate yet
    now[0] += 5
    b.update(1, m(1750 * 2**20), "gpu-1")
    assert b.view(1)["swap_in_rate"] == 150 * 2**20 and b.view(1)["swap_out_rate"] == 0
    now[0] += 5
    b.update(1, m(10 * 2**20), "gpu-1")                                   # the host rebooted: counters start again
    assert b.view(1)["swap_in_rate"] is None
    now[0] += board_mod.STALE + 1
    assert b.view(1) is None and b.views() == []                         # an old number would read as current
    b.update(2, None, "gpu-2")                                           # no reading: nothing to show
    assert b.view(2) is None


def test_metrics_lines_name_the_worker_and_leave_unknowns_out():
    now = [0.0]
    b = board_mod.MemoryBoard(clock=lambda: now[0])
    b.update(1, board_mod.from_extension({"ram_total": 64, "ram_available": 40, "swap_total": 8, "swap_used": 1,
                                          "swap_in": 0, "swap_out": 0, "rss": 5,
                                          "devices": [{"index": 0, "name": "R9700", "total": 32, "used": 23, "reserved": 22}]}),
             'gpu "1"')
    b.update(2, board_mod.from_system_stats({"system": {"ram_total": 32, "ram_free": 10},
                                             "devices": [{"type": "cuda", "index": 0, "vram_total": 16, "vram_free": 16,
                                                          "torch_vram_total": 3}]}), "old")
    text = board_mod.prometheus(b.views())
    assert 'comfyfarm_worker_ram_available_bytes{worker="gpu \\"1\\""} 40' in text
    assert 'comfyfarm_worker_vram_used_bytes{worker="gpu \\"1\\"",device="0"} 23' in text
    assert 'comfyfarm_worker_ram_available_bytes{worker="old"} 10' in text
    assert 'comfyfarm_worker_vram_reserved_bytes{worker="old",device="0"} 3' in text
    assert 'vram_used_bytes{worker="old"' not in text                    # mem_get_info's figure is never exported
    assert 'swap_used_bytes{worker="old"' not in text                    # unknown: absent, not 0


async def test_memory_shows_for_admins_users_of_the_group_and_metrics(dsn, fakes):
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            a, b = await fakes(ws_fix=True, secured=True), await fakes(ws_fix=True)   # a: with the extension
            a.fake.memory = {"ram_total": 60 * GiB, "ram_available": 31 * GiB, "swap_total": 31 * GiB, "swap_used": 4 * GiB,
                             "swap_in": 0, "swap_out": 0, "rss": 22 * GiB,
                             "devices": [{"index": 0, "name": "R9700", "total": 32 * GiB, "used": int(23.5 * GiB),
                                          "reserved": int(22.8 * GiB), "used_from": "driver"}]}
            g1, wa = await add_worker(r, a.url, group="g1", name="gpu-a")
            g2, wb = await add_worker(r, b.url, group="g2", name="gpu-b")      # extension without memory: RAM from stats
            await c.post("/api/admin/users", json={"username": "bob", "password": "password-1", "groups": [g1]},
                         headers=H("admin"))
            for _ in range(100):
                ws = {w["name"]: w for w in (await c.get("/api/admin/workers", headers=H("admin"))).json()}
                if ws["gpu-a"]["memory"] and ws["gpu-b"]["memory"] and ws["gpu-a"]["memory"]["swap_in_rate"] is not None:
                    break
                await asyncio.sleep(0.1)
            ma, mb = ws["gpu-a"]["memory"], ws["gpu-b"]["memory"]
            assert ma["from"] == "extension" and ma["devices"][0]["used"] == int(23.5 * GiB) and ma["swap_in_rate"] == 0
            assert mb["from"] == "system_stats" and mb["ram_available"] == 40 * GiB and mb["devices"][0]["used"] is None
            dash = (await c.get("/api/farm/dashboard", headers=H("bob"))).json()
            assert [g["name"] for g in dash["groups"]] == ["g1"]                    # bob sees his group's workers only
            assert [(m["name"], m["memory"]["ram_total"]) for m in dash["groups"][0]["members"]] == [("gpu-a", 60 * GiB)]
            text = (await c.get("/metrics")).text
            assert 'comfyfarm_worker_vram_used_bytes{worker="gpu-a",device="0"} 25232932864' in text
            assert 'comfyfarm_worker_ram_available_bytes{worker="gpu-b"} 42949672960' in text
