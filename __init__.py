# Standalone WikiHow Scraper Toolkit

import os
import json
import socket

# Root directory of the scraper package
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)

# --- All TUNABLE values live in settings.json, not here -------------------------
# This file's only job (besides being the package init) is computing filesystem
# paths (which JSON can't express - there's no equivalent of Python's __file__) and
# loading settings.json's values. Edit settings.json to change ports, selectors, or
# worker sizing - not this file.

SETTINGS_PATH = os.path.join(PACKAGE_DIR, "settings.json")

_SETTINGS_DEFAULTS = {
    "proxy": {"default_proxy_port": 9080, "default_control_port": 9081, "use_tor": False, "auto_connect": False},
    "workers": {
        "ram_buffer_mb": 1024,
        "estimated_mb_per_tab_worker": 150,
        "estimated_mb_per_process_worker": 350,
        "min_workers": 1,
        "max_workers_cap": 8,
    },
    # None/None = not yet configured via the dashboard's memory slider - falls back
    # to the legacy ram_buffer_mb-only behavior in get_adaptive_worker_count().
    "memory_slider": {"min_gb": None, "max_gb": None},
}


def _deep_merge_defaults(data, defaults):
    """Fills in any missing keys from defaults, one level deep per section - so a
    partially-edited or slightly out-of-date settings.json still works."""
    merged = dict(defaults)
    for section, values in defaults.items():
        if section in data and isinstance(data[section], dict):
            merged[section] = {**values, **data[section]}
    return merged


def load_settings():
    """Reads settings.json, falling back to built-in defaults for any missing/invalid keys."""
    try:
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
        return _deep_merge_defaults(data, _SETTINGS_DEFAULTS)
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


_settings = load_settings()

# Module-level constants used throughout the package.
DEFAULT_PROXY_PORT = _settings["proxy"]["default_proxy_port"]
DEFAULT_CONTROL_PORT = _settings["proxy"]["default_control_port"]


def find_free_port():
    """Finds a random free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


import sys
import shutil

# Cross-platform Tor executable resolution (Windows & macOS/Linux)
if sys.platform == "win32":
    TOR_EXE = os.path.join(PACKAGE_DIR, "dependencies", "tor-ip-changer", "tor", "tor_scraper.exe")
    GEOIP_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-ip-changer", "tor", "geoip")
    GEOIP6_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-ip-changer", "tor", "geoip6")
else:
    sys_tor = shutil.which("tor")
    mac_bundled = os.path.join(PACKAGE_DIR, "dependencies", "tor-mac", "bin", "tor")
    if os.path.exists(mac_bundled):
        TOR_EXE = mac_bundled
        GEOIP_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-mac", "share", "geoip")
        GEOIP6_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-mac", "share", "geoip6")
    elif sys_tor:
        TOR_EXE = sys_tor
        GEOIP_PATH = "/opt/homebrew/share/tor/geoip" if os.path.exists("/opt/homebrew/share/tor/geoip") else "/usr/local/share/tor/geoip"
        GEOIP6_PATH = "/opt/homebrew/share/tor/geoip6" if os.path.exists("/opt/homebrew/share/tor/geoip6") else "/usr/local/share/tor/geoip6"
    else:
        TOR_EXE = mac_bundled
        GEOIP_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-mac", "share", "geoip")
        GEOIP6_PATH = os.path.join(PACKAGE_DIR, "dependencies", "tor-mac", "share", "geoip6")

# PORTABLE: Root data directory - holds browser profiles and the scraped database
DATA_DIR = os.path.join(PACKAGE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# PORTABLE: Directory to save Chrome / Selenium user profiles
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)

# PORTABLE: Per-article revision data, one JSON file per article, grouped into
# folders by the article's top-level WikiHow category (see articles.py).
ARTICLES_DIR = os.path.join(DATA_DIR, "articles")
os.makedirs(ARTICLES_DIR, exist_ok=True)

# PORTABLE: Transient runtime state for spawned processes (currently: per-run Tor
# data dirs and torrc files - see proxy.py). Everything the package writes at
# runtime MUST live under PACKAGE_DIR/DATA_DIR, never BASE_DIR (this package's
# PARENT folder) - the whole point of PACKAGE_DIR/DATA_DIR is that this package
# can be shared/copied as a self-contained unit with no references outside itself.
TOR_RUNTIME_DIR = os.path.join(DATA_DIR, "tor_runtime")
os.makedirs(TOR_RUNTIME_DIR, exist_ok=True)

# PORTABLE: Directory for watchdog and runtime logs
LOGS_DIR = os.path.join(PACKAGE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Path to the active Tor ports registry
TOR_PORTS_REGISTRY = os.path.join(PROFILES_DIR, "active_tor_ports.json")


def get_adaptive_worker_count(mode="tab"):
    """
    Picks a worker count based on currently available RAM, settings.json's
    worker buffer/per-worker estimates, and a hard cap - so the scraper doesn't spawn
    more tabs/processes than this machine can actually handle.

    If the dashboard's memory slider has been set (settings.json's memory_slider),
    it OVERRIDES the legacy ram_buffer_mb-only behavior:
      - min_gb becomes the reserve floor (replaces ram_buffer_mb) - matches the
        slider's own visual semantics: usage crossing below this floor is what the
        widget flags red.
      - max_gb caps the TOTAL memory workers may draw on, even if more is actually
        free - lets the user deliberately hold some free RAM back rather than
        always maximizing worker count to whatever's currently available.
    Without a configured slider (min_gb/max_gb are None), falls back to the
    original behavior entirely, unchanged.

    mode: "tab" (workers = extra tabs in an existing browser, lighter) or
          "process" (workers = separate Chrome processes, heavier).
    """
    import psutil

    settings = load_settings()
    cfg = settings["workers"]
    slider = settings.get("memory_slider", {})
    min_gb, max_gb = slider.get("min_gb"), slider.get("max_gb")

    available_mb = psutil.virtual_memory().available / (1024 * 1024)

    if min_gb is not None and max_gb is not None:
        reserve_mb = min_gb * 1024
        budget_mb = max_gb * 1024
        usable_mb = max(0, min(available_mb - reserve_mb, budget_mb))
    else:
        usable_mb = max(0, available_mb - cfg["ram_buffer_mb"])

    per_worker = cfg["estimated_mb_per_tab_worker"] if mode == "tab" else cfg["estimated_mb_per_process_worker"]
    if per_worker <= 0:
        per_worker = 1

    workers = int(usable_mb // per_worker)
    workers = max(cfg["min_workers"], min(workers, cfg["max_workers_cap"]))
    return workers
