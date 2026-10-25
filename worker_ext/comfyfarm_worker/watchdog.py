"""The executor watchdog and the health report (field, dev.21: a GPU fault killed ComfyUI's prompt_worker thread; the
process stayed up, /queue kept answering 200, and every job sent to it waited forever).

ComfyUI runs prompts on one thread, started in main.py as threading.Thread(target=prompt_worker) — named
"Thread-N (prompt_worker)". An exception outside its per-prompt try (soft_empty_cache after a CUDA fault) ends it, and
nothing restarts it. The watchdog notices and exits the process: the service manager (Restart=on-failure) starts a
fresh ComfyUI within seconds, and the router puts back the jobs that never started. COMFYFARM_EXECUTOR_WATCHDOG=0
turns the exit off; GET /comfyfarm/health still says what it sees.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("comfyfarm")
EXECUTOR = "(prompt_worker)"
EXIT_CODE = 75                  # not 2: the unit's RestartPreventExitStatus is the launcher's refusal
PERIOD = 5.0
FIND_FOR = 900.0                # an executor not seen this long after load: a ComfyUI we do not know — stop looking


class Watch:
    """'unknown' until the executor thread has been seen, then 'alive' or 'dead' (it never comes back)."""

    def __init__(self):
        self.seen = False

    def check(self, threads=None) -> str:
        alive = any(EXECUTOR in t.name and t.is_alive() for t in (threading.enumerate() if threads is None else threads))
        if alive:
            self.seen = True
            return "alive"
        return "dead" if self.seen else "unknown"


WATCH = Watch()


def device() -> str:
    """'ok', 'cpu', or 'error: …' — a CUDA/HIP context broken by a fault fails every call, this one included."""
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        torch.cuda.mem_get_info()
        return "ok"
    except Exception as e:                                       # the report must not fail with what it reports
        return f"error: {type(e).__name__}: {e}"[:300]


def run(exit=os._exit, sleep=time.sleep, clock=time.monotonic, watch: Watch = WATCH, period: float = PERIOD):
    started = clock()
    while True:
        state = watch.check()
        if state == "dead":
            log.error("comfyfarm: ComfyUI's prompt executor thread has stopped — no job can run in this process; "
                      "exiting (code %d) so the service starts ComfyUI again", EXIT_CODE)
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
            exit(EXIT_CODE)
            return
        if state == "unknown" and clock() - started > FIND_FOR:
            log.info("comfyfarm: no prompt executor thread found — the watchdog stops looking")
            return
        sleep(period)


def start(environ=os.environ) -> bool:
    if environ.get("COMFYFARM_EXECUTOR_WATCHDOG", "1") == "0":
        return False
    threading.Thread(target=run, name="comfyfarm-watchdog", daemon=True).start()
    return True
