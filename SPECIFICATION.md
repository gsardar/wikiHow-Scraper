# WikiHow Scraper — Specification & Usage Guide

Standalone toolkit for scraping WikiHow articles through a self-managed Tor proxy and a
pool of self-healing, profile-authenticated Chrome browser instances. This document
treats this folder (`wikihow_scraper/`) as the root — all paths below are relative to it.

> This package is independent from the `wikihow/` library (the WikiHow API client + CLI
> used for the gender-research pipeline). Use this spec only for the scraper toolkit.

---

## 1. Folder Structure

```
wikihow_scraper/                  ← root
├── __init__.py                   # Package marker ("Standalone WikiHow Scraper Toolkit")
├── cli.py                        # argparse CLI + TUI entry point
├── config.py                     # Paths, ports, default CSS selectors
├── scraper.py                    # WikiHowScraper — HTTP + browser fetch/parse engine
├── workers.py                    # ScraperWorkerPool — multi-threaded batch scraping
├── profiles.py                   # ProfileManager — Chrome profile & login management
├── proxy.py                      # StandaloneTorManager (`tor`) — Tor process lifecycle
├── tui.py                        # launch_tui() — interactive dashboard
│
├── pid_tracker/
│   ├── __init__.py
│   └── pid_manager.py            # BrowserWatchdog — self-healing browser monitor (CDP)
│
├── dependencies/
│   └── tor-ip-changer/           # Bundled portable Tor executable + GeoIP data
│       └── tor/
│           ├── tor_scraper.exe
│           ├── geoip
│           └── geoip6
│
├── profiles_data/                # Chrome user-data-dirs, one per named profile
│   ├── <profile_name>/
│   │   └── account_info.json     # provider / account_name / email / last_updated
│   └── active_tor_ports.json     # Registry of the currently running Tor proxy/control ports
│
└── logs/
    ├── browser_mgmt.log
    ├── process_lifecycle.json
    └── watchdog_<profile>.log
```

---

## 2. Requirements

Install from the project root:

```bash
pip install -r requirements.txt
```

Covers: `requests`, `beautifulsoup4`, `rich`, `seleniumbase`, `nameparser`, `transformers`, `pillow`.

**Not currently pinned in `requirements.txt` but required by this package** — install manually if missing:
```bash
pip install psutil stem websockets
```
- `psutil` — process discovery/kill for Tor and Chrome
- `stem` — Tor control-port signaling (IP rotation)
- `websockets` — used by the browser watchdog for CDP communication

The bundled Tor binary at `dependencies/tor-ip-changer/tor/tor_scraper.exe` must be present
for any `proxy` command or Tor-backed scrape to work (Windows only, portable — no system
Tor install needed).

---

## 3. Running It

From the project root, invoked as a module:

```bash
python -m wikihow_scraper <command> ...
```

Running with **no arguments** launches the interactive TUI dashboard directly:

```bash
python -m wikihow_scraper
```

### 3.1 `scrape` — Download article content

```bash
# Single article, via Tor + plain HTTP requests
python -m wikihow_scraper scrape "Tie-a-Tie"

# Single article, no proxy
python -m wikihow_scraper scrape "Tie-a-Tie" --no-proxy

# Single article, via a real (headless) browser instead of requests
python -m wikihow_scraper scrape "Tie-a-Tie" --browser

# Multiple articles in parallel (comma-separated titles)
python -m wikihow_scraper scrape "Tie-a-Tie,Bake-a-Cake,Fix-a-Leak" --multi --workers 5

# Custom CSS selectors (JSON) instead of the defaults
python -m wikihow_scraper scrape "Tie-a-Tie" --selectors '{"title": "h1", "steps": ".step"}'
```

| Flag | Default | Meaning |
|---|---|---|
| `target` | — | Article title, or comma-separated list with `--multi` |
| `--multi` | off | Enable `ScraperWorkerPool` batch mode |
| `--workers N` | `3` | Parallel worker threads (`--multi` only) |
| `--selectors JSON` | built-in | Override `DEFAULT_SELECTORS` |
| `--no-proxy` | off (Tor on) | Disable the Tor SOCKS proxy |
| `--browser` | off (HTTP mode) | Fetch via a headless SeleniumBase Chrome instance instead of `requests` |

Default selectors (`config.py`):
```python
{
    "title": "h1",
    "steps": ".step",
    "categories": ".pb-badge",
    "author_bio": "#user_about",
}
```

**Fetch behavior**: HTTP mode retries up to 5 times, rotating the Tor circuit on a
403/429/503 or a Cloudflare/"Access Denied" page body. Browser mode launches a fresh
headless Chrome per call, applies the same Tor proxy if enabled, and closes the driver
after grabbing `page_source`.

### 3.2 `proxy` — Manage the Tor SOCKS proxy

```bash
python -m wikihow_scraper proxy connect    # launch tor_scraper.exe on random free ports
python -m wikihow_scraper proxy status     # show status, ports, current exit IP
python -m wikihow_scraper proxy rotate     # send NEWNYM signal, get a new exit IP
python -m wikihow_scraper proxy shutdown   # kill the Tor process, clear the port registry
```

- Ports are chosen dynamically per `connect` call and written to
  `profiles_data/active_tor_ports.json` so `scraper.py` and other commands can discover them.
- `rotate` requires the control port to be reachable and authenticates without a password
  (`CookieAuthentication 0`).
- Verification pings `https://api.ipify.org` through the SOCKS proxy.

### 3.3 `profile` — Manage Chrome profiles / logged-in accounts

```bash
python -m wikihow_scraper profile list
python -m wikihow_scraper profile add --name research_1
python -m wikihow_scraper profile login --name research_1        # opens a visible Chrome window
python -m wikihow_scraper profile check-login --name research_1  # headless check; auto-prompts login if not logged in
python -m wikihow_scraper profile delete --name research_1
python -m wikihow_scraper profile tui                             # menu-driven version of the above
```

Each profile is a persistent Chrome `user_data_dir` under `profiles_data/<name>/`, plus an
`account_info.json` recording the detected login provider (WikiHow / Google / Facebook),
account name, and last-updated timestamp. Login detection looks for
`#header_user_profile`, `.logged-in`, `#user_about`, or an `action=logout` link in the page.

### 3.4 `browser` — Self-healing browser watchdog

```bash
python -m wikihow_scraper browser start research_1 --port 9099
python -m wikihow_scraper browser status research_1
python -m wikihow_scraper browser stop research_1
```

`BrowserWatchdog` (in `pid_tracker/pid_manager.py`) launches Chrome via SeleniumBase bound
to a given profile, exposes a CDP debug port, tracks open tabs, and logs to
`logs/watchdog_<profile>.log`. Use this when you want a long-lived, monitored browser
rather than the short-lived instances `scrape --browser` spins up on demand.

### 3.5 `tui` — Interactive dashboard

```bash
python -m wikihow_scraper tui
```
Equivalent to running the module with no arguments at all.

---

## 4. Programmatic Usage

The CLI is a thin wrapper — everything is usable directly as a library:

```python
from wikihow_scraper.proxy import tor
from wikihow_scraper.scraper import WikiHowScraper
from wikihow_scraper.workers import ScraperWorkerPool
from wikihow_scraper.profiles import ProfileManager

tor.connect()
scraper = WikiHowScraper(use_tor=True)
content = scraper.get_article_content("Tie-a-Tie")

pool = ScraperWorkerPool(max_workers=5, use_tor=True)
results = pool.download_articles(["Tie-a-Tie", "Bake-a-Cake"])

ProfileManager.add_profile("research_1")
ProfileManager.interactive_login("research_1")
```

`ScraperWorkerPool.download_articles(titles, profile_mapping=None)` accepts an optional
`profile_mapping` (a `list` cycled round-robin across titles, or a `dict` of
`title -> profile_name`) to bind specific workers to specific authenticated Chrome
profiles — useful for spreading load across multiple logged-in accounts.

---

## 5. Notes & Gotchas

- **Windows-only as-is**: the bundled Tor binary is a `.exe`, and `proxy.py` uses Windows
  process-creation flags (`0x00000008 | 0x08000000`) to hide the console window.
- **Per-worker ports**: `ScraperWorkerPool` assigns each worker a CDP debug port starting
  at `9100 + worker_index` to avoid collisions when running `--browser --multi` together.
- **Tor scope**: only page-fetch traffic is proxied through Tor; there is no separate Tor
  path for anything else in this package.
- **Selector fragility**: `DEFAULT_SELECTORS` are plain CSS selectors against WikiHow's
  live markup (`h1`, `.step`, `.pb-badge`, `#user_about`) — if WikiHow changes its page
  structure, pass `--selectors` with updated values rather than editing `config.py` for a
  one-off run.
- **`profiles_data/` and `logs/` are runtime state**, not source — safe to delete to reset
  all saved logins and Tor port registrations (you'll need to re-run `profile login` for
  each account afterward).
