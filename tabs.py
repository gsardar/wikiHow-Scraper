"""
Tab hygiene utilities for the shared, long-running watchdog browser instance.

Concurrent tab-workers (history.py, articles.py) have twice now produced orphaned
"about:blank" tabs and cross-contaminated data from a window.open()+window_handles[-1]
race (a thread grabbing a tab a DIFFERENT thread just opened). Both call sites are
fixed to avoid causing new orphans, but this module gives a quick way to audit and
clean up the shared browser's tab list - useful after any concurrent run, or anytime
tab state looks suspicious.
"""

import os
import glob
import platform
import time
import threading
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# SHARED across every module that opens tabs in the watchdog browser (history.py,
# articles.py, rich_revision.py, article_pipeline.py). Each used to define its OWN
# separate threading.Lock() - which does NOT provide mutual exclusion between
# DIFFERENT modules' tab-creation code running concurrently (e.g. two concurrent
# scrape_article_to_json() calls, one calling into history.py while the other calls
# into rich_revision.py, would race against each other despite each individually
# being "locked"). One shared lock, imported everywhere, is required for real safety.
TAB_CREATE_LOCK = threading.Lock()


def attach_driver(port=9099):
    """
    Attaches a Selenium driver to the shared watchdog Chrome instance via CDP.
    Uses Selenium Manager to automatically match the installed Chrome browser version.
    """
    options = Options()
    options.debugger_address = f"127.0.0.1:{port}"
    return webdriver.Chrome(options=options)


def _attach(port=9099):
    return attach_driver(port)


def claim_new_tab(driver):
    """
    Opens a brand-new tab and returns its handle, guaranteed to be one THIS caller
    opened (not one a concurrent thread just created) - diffs the handle set before/
    after under the shared TAB_CREATE_LOCK. Does not switch to it; caller should do
    driver.switch_to.window(handle) itself.
    """
    with TAB_CREATE_LOCK:
        before = set(driver.window_handles)
        driver.execute_script("window.open('about:blank', '_blank');")
        time.sleep(0.3)
        new_handles = set(driver.window_handles) - before
        return new_handles.pop() if new_handles else driver.window_handles[-1]


def _is_orphan_url(url):
    return url in ("about:blank", "") or url.startswith("data:")


def detach_driver_safely(driver):
    """Stops local chromedriver service for an attached session without sending Browser.close CDP command to Chrome."""
    if not driver:
        return
    try:
        if hasattr(driver, "service") and driver.service:
            driver.service.stop()
    except Exception:
        pass
    try:
        driver.session_id = None
    except Exception:
        pass


def list_tabs(port=9099):
    """
    Returns a list of {handle, url, title} for every open tab, without changing focus.

    Attaches its own short-lived driver and always quits it before returning - every
    _attach()/attach_driver() call spawns a brand-new chromedriver SERVER PROCESS that
    stays attached to the shared watchdog Chrome via CDP until quit() is called. Left
    unquit (as every function below originally did), these accumulate one per call and
    have been confirmed (on macOS) to eventually make a NEW attach attempt hang
    indefinitely - almost certainly too many concurrent CDP debugger sessions on the
    same target. Read-only/short helper functions like this one MUST clean up after
    themselves; only a caller that needs to keep driving a tab across multiple actions
    (history.py, articles.py, rich_revision.py, article_pipeline.py) should hold onto
    a driver beyond one function call, and those already quit() in their own finally.
    """
    driver = _attach(port)
    try:
        tabs = []
        for h in driver.window_handles:
            driver.switch_to.window(h)
            tabs.append({"handle": h, "url": driver.current_url, "title": driver.title})
        return tabs
    finally:
        detach_driver_safely(driver)


def find_orphan_tabs(port=9099):
    """
    Flags tabs that look like leftovers rather than intentional state:
    - about:blank (never navigated after being opened)
    - data: URLs (rare, but similarly "nobody finished setting this up")
    Does NOT flag a single blank tab if it's the ONLY tab (that's just an empty
    browser, not an orphan from a race).
    """
    tabs = list_tabs(port)
    if len(tabs) <= 1:
        return []
    return [t for t in tabs if _is_orphan_url(t["url"])]


def close_orphan_tabs(port=9099, dry_run=False):
    """
    Closes tabs that look like leftovers (see _is_orphan_url). Always leaves at least
    one tab open (refuses to close the last remaining tab even if it happens to be
    blank). Returns the list of {handle, url, title} that were (or would be, if
    dry_run) closed.
    """
    driver = _attach(port)
    try:
        tabs = []
        for h in driver.window_handles:
            driver.switch_to.window(h)
            tabs.append({"handle": h, "url": driver.current_url, "title": driver.title})

        orphans = [] if len(tabs) <= 1 else [t for t in tabs if _is_orphan_url(t["url"])]
        if len(orphans) >= len(tabs):
            orphans = orphans[:-1]  # never close the very last tab

        if not dry_run:
            for t in orphans:
                try:
                    driver.switch_to.window(t["handle"])
                    driver.close()
                except Exception:
                    pass
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])

        return orphans
    finally:
        detach_driver_safely(driver)


def close_all_residual_tabs(port=9099, dry_run=False):
    """
    Closes EVERY open tab except one - not just the Main-Page anchor (see module
    history: this used to be close_idle_home_tabs(), scoped to Main-Page only; a
    scraping run can also leave behind work-tabs from a stop/crash mid-fetch, so the
    real need is "clean slate", not "just the home page"). Same "never close the
    last tab" safety as close_orphan_tabs()/the old home-tab version: the watchdog's
    health check just probes whatever tab is first in Chrome's own list, not any
    specific URL (confirmed by reading pid_manager.py), so closing down to one
    remaining tab is always safe.

    Call this at the START of a run (clears whatever was left open before, e.g. the
    Main-Page anchor) AND at the END (clears any work-tabs the run itself opened, in
    case something closed abnormally without cleaning up after itself) - never when
    it would leave zero tabs, which is handled automatically.
    """
    driver = _attach(port)
    try:
        tabs = []
        for h in driver.window_handles:
            driver.switch_to.window(h)
            tabs.append({"handle": h, "url": driver.current_url, "title": driver.title})

        to_close = tabs[1:] if len(tabs) > 1 else []  # keep exactly one tab, close the rest

        if not dry_run:
            for t in to_close:
                try:
                    driver.switch_to.window(t["handle"])
                    driver.close()
                except Exception:
                    pass
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])

        return to_close
    finally:
        detach_driver_safely(driver)


def audit_report(port=9099):
    """Human-readable summary: total tabs, orphans found, and each tab's state."""
    tabs = list_tabs(port)
    orphans = [t for t in tabs if _is_orphan_url(t["url"])] if len(tabs) > 1 else []
    lines = [f"{len(tabs)} tab(s) open, {len(orphans)} flagged as orphaned:"]
    for t in tabs:
        flag = " [ORPHAN]" if t in orphans else ""
        lines.append(f"  {t['handle']}: {t['url']}{flag}")
    return "\n".join(lines)
