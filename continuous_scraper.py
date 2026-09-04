"""
Queue-driven article scraping, two modes:
  - run_once(titles, ...)       process a specific list once, then stop ("List" mode)
  - run_continuous(...)          keep pulling from the pending.txt queue forever,
                                  until stop_continuous() is called ("Continuous" mode)

Both share the same worker/progress-tracking machinery. "Single article" mode (the
TUI's third option) doesn't need this module at all - it just calls
article_pipeline.scrape_article_to_json() directly for one title.

Queue files live in data/queue/:
  - pending.txt    append article titles here any time, one per line
  - completed.txt  titles that finished successfully (auto-maintained)
  - failed.txt     "<title>\t<error>" for titles that raised an exception (auto-maintained)
"""

import os
import time
import threading

from wikihow_scraper import DATA_DIR, get_adaptive_worker_count
from wikihow_scraper.article_pipeline import scrape_article_to_json
from wikihow_scraper.tabs import close_all_residual_tabs

QUEUE_DIR = os.path.join(DATA_DIR, "queue")
os.makedirs(QUEUE_DIR, exist_ok=True)
PENDING_FILE = os.path.join(QUEUE_DIR, "pending.txt")
COMPLETED_FILE = os.path.join(QUEUE_DIR, "completed.txt")
FAILED_FILE = os.path.join(QUEUE_DIR, "failed.txt")

for f in (PENDING_FILE, COMPLETED_FILE, FAILED_FILE):
    if not os.path.exists(f):
        open(f, "a").close()

_state_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress = {}  # title -> {"current": int, "total": int, "start_time": float}

_stop_event = threading.Event()
_running_lock = threading.Lock()
_is_running = False


def get_progress():
    """Snapshot of {title: {current, total, start_time}} for articles currently being
    scraped - for a live dashboard (TUI) to poll. Empty once a batch finishes."""
    with _progress_lock:
        return {k: dict(v) for k, v in _progress.items()}


def get_overall_rate():
    """Aggregate revisions/sec across every article currently being scraped, summed
    from each article's own (current fetched) / (elapsed time since it started)."""
    now = time.time()
    total_rate = 0.0
    for info in get_progress().values():
        elapsed = now - info["start_time"]
        if elapsed > 0:
            total_rate += info["current"] / elapsed
    return total_rate


def is_running():
    with _running_lock:
        return _is_running


def stop():
    """Signals any active run_continuous()/run_once() loop to stop after its current
    batch finishes. Safe to call whether or not a run is active."""
    _stop_event.set()


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def add_to_queue(*titles):
    """Appends one or more article titles to pending.txt. Safe to call while a run
    is already active - it re-reads the file each cycle."""
    with _state_lock:
        with open(PENDING_FILE, "a", encoding="utf-8") as f:
            for t in titles:
                f.write(t + "\n")


def _mark_completed(title):
    with _state_lock:
        with open(COMPLETED_FILE, "a", encoding="utf-8") as f:
            f.write(title + "\n")


def _mark_failed(title, error):
    with _state_lock:
        with open(FAILED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{title}\t{error}\n")


def get_queue_status():
    """Counts for a dashboard: pending (not yet done/failed), completed, failed."""
    pending = _read_lines(PENDING_FILE)
    done = set(_read_lines(COMPLETED_FILE))
    failed_lines = _read_lines(FAILED_FILE)
    failed = set(line.split("\t")[0] for line in failed_lines)
    remaining = [t for t in pending if t not in done and t not in failed]
    return {"pending": len(remaining), "completed": len(done), "failed": len(failed)}


def _next_batch(candidates, batch_size):
    """Filters `candidates` down to ones not already completed/failed, capped at
    batch_size. `candidates` can be pending.txt's contents or an explicit title list."""
    done = set(_read_lines(COMPLETED_FILE))
    failed = set(line.split("\t")[0] for line in _read_lines(FAILED_FILE))
    remaining = [t for t in candidates if t not in done and t not in failed]
    return remaining[:batch_size]


def _scrape_one(title, port, max_revisions):
    def on_progress(current, total):
        with _progress_lock:
            if title in _progress:
                _progress[title]["current"] = current
                _progress[title]["total"] = total

    with _progress_lock:
        _progress[title] = {"current": 0, "total": max_revisions or 0, "start_time": time.time()}

    try:
        scrape_article_to_json(title, port=port, max_revisions=max_revisions, progress_callback=on_progress,
                                should_stop=_stop_event.is_set)
        if _stop_event.is_set():
            _mark_failed(title, "stopped by user (partial progress saved)")
        else:
            _mark_completed(title)
    except Exception as e:
        _mark_failed(title, str(e))
    finally:
        with _progress_lock:
            _progress.pop(title, None)


def run_once(titles, port=9099, max_workers=None, max_revisions=None):
    """
    "List" mode: scrape exactly this list of titles, concurrently (adaptive worker
    count if not given), then return - does not keep polling for more.
    """
    global _is_running
    with _running_lock:
        _is_running = True
    _stop_event.clear()
    close_all_residual_tabs(port=port)
    add_to_queue(*titles)

    try:
        workers = max_workers if max_workers is not None else get_adaptive_worker_count(mode="tab")
        remaining = _next_batch(titles, len(titles))
        for i in range(0, len(remaining), workers):
            if _stop_event.is_set():
                break
            batch = remaining[i:i + workers]
            threads = [threading.Thread(target=_scrape_one, args=(t, port, max_revisions)) for t in batch]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    finally:
        # End-of-run cleanup: any batch's threads have all joined by now (no tab is
        # legitimately still in use), so this is a safe point to reduce back down to
        # one tab - clears anything a stop/crash mid-fetch left open.
        close_all_residual_tabs(port=port)
        with _running_lock:
            _is_running = False


def run_continuous(port=9099, max_workers=None, max_revisions=None, poll_interval=15, run_seconds=None):
    """
    "Continuous" mode: repeatedly pulls a batch of not-yet-processed titles from
    pending.txt, scrapes them concurrently, then sleeps poll_interval seconds before
    checking for more. Runs until stop() is called, or run_seconds elapses (useful
    for bounded test runs), or forever otherwise.
    """
    global _is_running
    with _running_lock:
        _is_running = True
    _stop_event.clear()
    close_all_residual_tabs(port=port)
    start = time.time()
    print(f"[continuous] watching {PENDING_FILE} - add titles there any time.")

    try:
        while True:
            if _stop_event.is_set():
                print("[continuous] stop() called, stopping.")
                return
            if run_seconds and (time.time() - start) > run_seconds:
                print("[continuous] run_seconds elapsed, stopping.")
                return

            workers = max_workers if max_workers is not None else get_adaptive_worker_count(mode="tab")
            batch = _next_batch(_read_lines(PENDING_FILE), workers)

            if not batch:
                print(f"[continuous] queue empty, auto-discovering new articles...")
                try:
                    from wikihow_scraper.discovery import random_articles
                    new_titles = random_articles(n=20)
                    if new_titles:
                        add_to_queue(*new_titles)
                        batch = _next_batch(_read_lines(PENDING_FILE), workers)
                except Exception as e:
                    print(f"[continuous] auto-discovery warning: {e}")

            if not batch:
                print(f"[continuous] queue empty, sleeping {poll_interval}s...")
                for _ in range(poll_interval):
                    if _stop_event.is_set():
                        return
                    time.sleep(1)
                continue

            print(f"[continuous] processing batch of {len(batch)} (workers={workers}): {batch}")
            threads = [threading.Thread(target=_scrape_one, args=(t, port, max_revisions)) for t in batch]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    finally:
        close_all_residual_tabs(port=port)
        with _running_lock:
            _is_running = False
