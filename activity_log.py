"""
Shared in-memory activity log, used by both the TUI and the web dashboard so job
results (scrapes, updates, queue actions, proxy actions) show up in whichever
surface is open - or both, if run at the same time.
"""

import threading
import time

_lock = threading.Lock()
_lines = []  # list of {"time": float, "message": str}
MAX_LINES = 500


def log(message):
    with _lock:
        _lines.append({"time": time.time(), "message": message})
        del _lines[:-MAX_LINES]


def get_lines(limit=100):
    with _lock:
        return list(_lines[-limit:])
