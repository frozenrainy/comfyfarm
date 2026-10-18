"""Logging (docs/design/06 'Logs'): stdout for the service manager; with --log-dir also a size-rotated main log and
a separate <name>-error.log holding real failures only — one pair per program (router, gateway), since two processes
rotating the same file rename it under each other — a level split is a stronger filter than regular expressions in a
monitoring system."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
ROTATE_BYTES, ROTATE_KEEP = 20 * 1024 * 1024, 5


_mine: list[logging.Handler] = []        # handlers this module installed (others, e.g. a test runner's, are left alone)


def teardown() -> None:
    root = logging.getLogger()
    for h in _mine:
        root.removeHandler(h)
        h.close()
    _mine.clear()


def setup(log_dir: str | None = None, level: str = "INFO", name: str = "router") -> None:
    teardown()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(FORMAT)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    _mine.append(out)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        main = logging.handlers.RotatingFileHandler(os.path.join(log_dir, f"{name}.log"), maxBytes=ROTATE_BYTES,
                                                    backupCount=ROTATE_KEEP, encoding="utf-8")
        main.setFormatter(fmt)
        root.addHandler(main)
        _mine.append(main)
        err = logging.handlers.RotatingFileHandler(os.path.join(log_dir, f"{name}-error.log"), maxBytes=ROTATE_BYTES,
                                                   backupCount=ROTATE_KEEP, encoding="utf-8")
        err.setFormatter(fmt)
        err.setLevel(logging.ERROR)
        root.addHandler(err)
        _mine.append(err)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)     # one line per request is noise, not signal
