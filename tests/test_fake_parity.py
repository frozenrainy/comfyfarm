"""The fake worker must give the same protocol-probe verdicts as real ComfyUI (docs/research/*.txt).
A fake that disagrees with reality would make router tests pass for the wrong reasons."""
import pathlib, socket, subprocess, sys, threading, time

import pytest
from aiohttp import web

from comfyfarm.testing.fake_worker import FakeWorker
from probe_verdicts import verdicts

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE = {False: ROOT / "docs/research/probe-0.41.0-cpu.txt", True: ROOT / "docs/research/probe-0.41.0-cpu-with-ext.txt"}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def serve(fake, port):
    import asyncio
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(fake.app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
    loop.run_forever()


@pytest.mark.parametrize("ws_fix", [False, True], ids=["stock", "with-extension"])
def test_probe_parity(ws_fix):
    port = free_port()
    threading.Thread(target=serve, args=(FakeWorker(ws_fix=ws_fix), port), daemon=True).start()
    time.sleep(0.5)
    r = subprocess.run([sys.executable, str(ROOT / "spikes/protocol_probe.py"), f"http://127.0.0.1:{port}"],
                       capture_output=True, text=True, timeout=600)
    got, want = verdicts(r.stdout), verdicts(REFERENCE[ws_fix].read_text())
    assert want, "reference probe output missing or unparsable"
    assert got == want, f"fake differs from real ComfyUI:\n got  {got}\n want {want}"
