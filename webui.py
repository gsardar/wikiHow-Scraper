"""
Web dashboard - same capabilities as tui.py (mode selection, start/stop, live
accounts/queue/rates, activity log), built on the same backend modules
(continuous_scraper, article_pipeline, profiles, proxy, discovery), so the two
surfaces never drift apart or duplicate logic. The frontend polls /api/status
every 2s rather than using WebSockets - simpler and robust enough for a local
single-operator tool.
"""

import threading
import time
import psutil

from flask import Flask, jsonify, request, Response

import json as _json
from wikihow_scraper import SETTINGS_PATH
from wikihow_scraper.proxy import tor
from wikihow_scraper.profiles import ProfileManager
from wikihow_scraper.article_pipeline import scrape_article_to_json, update_article_json
from wikihow_scraper import continuous_scraper as cs
from wikihow_scraper import activity_log
from wikihow_scraper.discovery import (
    random_articles, recent_changes, random_in_category,
    new_pages, most_revisions, fewest_revisions, ancient_pages,
)

app = Flask(__name__)

# WikiHow's real top-level categories (confirmed via Special:CategoryListing) - used
# to populate the Filter dropdown with EXACT valid category slugs, since
# random_in_category()/category_articles() silently fail on made-up names.
TOP_LEVEL_CATEGORIES = [
    ("Arts and Entertainment", "Arts-and-Entertainment"),
    ("Cars & Other Vehicles", "Cars-%26-Other-Vehicles"),
    ("Computers and Electronics", "Computers-and-Electronics"),
    ("Education and Communications", "Education-and-Communications"),
    ("Family Life", "Family-Life"),
    ("Finance and Business", "Finance-and-Business"),
    ("Food and Entertaining", "Food-and-Entertaining"),
    ("Health", "Health"),
    ("Hobbies and Crafts", "Hobbies-and-Crafts"),
    ("Holidays and Traditions", "Holidays-and-Traditions"),
    ("Home and Garden", "Home-and-Garden"),
    ("Personal Care and Style", "Personal-Care-and-Style"),
    ("Pets and Animals", "Pets-and-Animals"),
    ("Philosophy and Religion", "Philosophy-and-Religion"),
    ("Relationships", "Relationships"),
    ("Sports and Fitness", "Sports-and-Fitness"),
    ("Travel", "Travel"),
    ("Work World", "Work-World"),
    ("Youth", "Youth"),
]

_accounts_cache = {"rows": [], "checked_at": 0}
_accounts_lock = threading.Lock()
_accounts_refresh_running = False
_accounts_refresh_pending = False

# Last login ATTEMPT outcome per profile (success/error/timeout + the detail message -
# e.g. "Incorrect password" from WikiHow's own error box). Login runs fire-and-forget
# in a background thread (the webui click returns immediately), and the scrolling
# activity log alone isn't a durable place to notice a failure - it scrolls away.
# Keyed separately from the logged_in/logged_out status so a past failure stays
# visible on the account row even after the row's live status is next refreshed.
_last_login_result = {}
_last_login_lock = threading.Lock()


def _refresh_accounts_async():
    """
    Serialized, not just "latest result wins": each check launches a real (slow)
    headless Chrome per profile via check_login_status(). Two overlapping checks for
    the SAME profile is a real correctness bug, not just wasted work - confirmed by
    reproducing it: deleting a profile while an earlier check for it was still
    in-flight let that check's Chrome launch RECREATE the just-deleted directory
    (Chrome creates user_data_dir if missing). A generation-counter alone only
    discards the stale RESULT; it doesn't stop the stale browser launch from
    touching disk. So only one refresh ever runs at a time; a call that arrives
    while one is running just flags a pending re-run (which reads the folder fresh
    again once the current pass finishes) instead of starting a second one.
    """
    global _accounts_refresh_running, _accounts_refresh_pending

    with _accounts_lock:
        if _accounts_refresh_running:
            _accounts_refresh_pending = True
            return
        _accounts_refresh_running = True

    def worker():
        global _accounts_refresh_running, _accounts_refresh_pending
        while True:
            rows = []
            for p in ProfileManager.list_profiles():
                name = p["profile_name"]
                method = p.get("provider", "Unknown")
                with _last_login_lock:
                    last_login = _last_login_result.get(name)
                try:
                    is_logged_in, _ = ProfileManager.check_login_status(name, auto_prompt_login=False)
                except Exception as e:
                    rows.append({"name": name, "method": method, "status": "error", "detail": str(e), "last_login": last_login})
                    continue
                if is_logged_in and last_login and last_login["ok"]:
                    # A successful attempt is fully explained by the LOGGED IN status
                    # itself - stop carrying it forward so it doesn't linger forever.
                    with _last_login_lock:
                        _last_login_result.pop(name, None)
                    last_login = None
                rows.append({"name": name, "method": method, "status": "logged_in" if is_logged_in else "logged_out", "last_login": last_login})

            with _accounts_lock:
                _accounts_cache["rows"] = rows
                _accounts_cache["checked_at"] = time.time()
                if _accounts_refresh_pending:
                    _accounts_refresh_pending = False
                    continue  # a create/rename/delete/etc. arrived mid-check - run once more
                _accounts_refresh_running = False
                return

    threading.Thread(target=worker, daemon=True).start()


_SORT_TYPE_FUNCS = {
    "new": new_pages,
    "newest_edits": recent_changes,
    "most_edited": most_revisions,
    "fewest_edited": fewest_revisions,
    "oldest": ancient_pages,
}


def _resolve_batch_titles(strategy, param, sort_type=None, category=None):
    if strategy == "sorted":
        # Filter doesn't apply here - WikiHow's site-wide ranked reports (NewPages,
        # MostRevisions, etc.) don't support category scoping. Silently ignored
        # rather than erroring, since the UI already disables the filter dropdown
        # for this strategy - a stale `category` value shouldn't block a run.
        n = int(param) if str(param).isdigit() and int(param) > 0 else 50
        func = _SORT_TYPE_FUNCS.get(sort_type, new_pages)
        return func(n)
    elif strategy == "random":
        n = int(param) if str(param).isdigit() and int(param) > 0 else 50
        if category:
            return random_in_category(category, n)
        return random_articles(n)
    else:  # manual
        titles = [t.strip() for t in str(param).split(",") if t.strip()]
        if not titles:
            raise ValueError("At least one article title required.")
        return titles


@app.route("/api/settings/memory_range", methods=["GET"])
def api_get_memory_range():
    """Persisted min/max worker-memory-budget slider positions, in GB - stored
    server-side (settings.json) rather than browser localStorage, so they survive
    across machines/browsers, not just this one viewer's local storage."""
    try:
        with open(SETTINGS_PATH, "r") as f:
            data = _json.load(f)
        rng = data.get("memory_slider")
        if rng:
            return jsonify({"min_gb": rng.get("min_gb"), "max_gb": rng.get("max_gb")})
    except Exception:
        pass
    return jsonify({"min_gb": None, "max_gb": None})


@app.route("/api/settings/memory_range", methods=["POST"])
def api_set_memory_range():
    data = request.get_json(force=True)
    min_gb = data.get("min_gb")
    max_gb = data.get("max_gb")
    try:
        with open(SETTINGS_PATH, "r") as f:
            settings_data = _json.load(f)
    except Exception:
        settings_data = {}
    settings_data["memory_slider"] = {"min_gb": min_gb, "max_gb": max_gb}
    with open(SETTINGS_PATH, "w") as f:
        _json.dump(settings_data, f, indent=2)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    now = time.time()
    progress = cs.get_progress()
    articles = []
    for title, info in progress.items():
        elapsed = max(now - info["start_time"], 0.01)
        rate = info["current"] / elapsed
        articles.append({
            "title": title, "current": info["current"], "total": info["total"],
            "rate": round(rate, 3),
        })

    with _accounts_lock:
        accounts = list(_accounts_cache["rows"])
        accounts_age = now - _accounts_cache["checked_at"] if _accounts_cache["checked_at"] else None

    tor_info = tor.get_status()

    vm = psutil.virtual_memory()
    memory = {
        "total_gb": round(vm.total / (1024 ** 3), 2),
        "used_gb": round((vm.total - vm.available) / (1024 ** 3), 2),
        "available_gb": round(vm.available / (1024 ** 3), 2),
    }

    return jsonify({
        "running": cs.is_running(),
        "queue": cs.get_queue_status(),
        "overall_rate": round(cs.get_overall_rate(), 3),
        "articles": articles,
        "accounts": accounts,
        "accounts_age_seconds": accounts_age,
        "tor": tor_info,
        "log": activity_log.get_lines(80),
        "memory": memory,
    })


@app.route("/api/accounts/refresh", methods=["POST"])
def api_accounts_refresh():
    _refresh_accounts_async()
    return jsonify({"ok": True})


@app.route("/api/profiles/create", methods=["POST"])
def api_profiles_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    mode = data.get("mode", "anonymous")  # "anonymous" | "direct" | "google" | "facebook"
    username = data.get("username", "")
    password = data.get("password", "")

    if not name:
        return jsonify({"ok": False, "error": "Profile name required."}), 400

    ok, msg = ProfileManager.add_profile(name)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    activity_log.log(f"[profile] created '{name}'")

    if mode != "anonymous":
        if not username or not password:
            return jsonify({"ok": True, "warning": "Profile created, but username/password were "
                                                     "missing so no credentials were saved."})
        ProfileManager.save_credentials(name, username, password, mode=mode)
        activity_log.log(f"[profile] saved {mode} credentials for '{name}', attempting auto-login...")

        def worker():
            _ensure_watchdog_running(port=9099)
            ok2, msg2 = ProfileManager.login(name)
            activity_log.log(f"[profile] login '{name}': {msg2}")
            _refresh_accounts_async()

        threading.Thread(target=worker, daemon=True).start()
    else:
        activity_log.log(f"[profile] '{name}' created anonymous - log in manually from the Accounts tab when ready.")

    _refresh_accounts_async()
    return jsonify({"ok": True})


@app.route("/api/profiles/rename", methods=["POST"])
def api_profiles_rename():
    data = request.get_json(silent=True) or {}
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"ok": False, "error": "Both old_name and new_name required."}), 400

    ok, msg = ProfileManager.rename_profile(old_name, new_name)
    activity_log.log(f"[profile] rename: {msg}")
    _refresh_accounts_async()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/profiles/delete", methods=["POST"])
def api_profiles_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    ok, msg = ProfileManager.delete_profile(name)
    activity_log.log(f"[profile] delete: {msg}")
    _refresh_accounts_async()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/profiles/login", methods=["POST"])
def api_profiles_login():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    def worker():
        with _last_login_lock:
            _last_login_result.pop(name, None)  # clear any stale prior result while this attempt is in flight
        try:
            _ensure_watchdog_running(port=9099)
            ok, msg = ProfileManager.login(name)
        except Exception as e:
            ok, msg = False, f"Login crashed: {e}"
        activity_log.log(f"[profile] login '{name}': {msg}")
        with _last_login_lock:
            _last_login_result[name] = {"ok": ok, "message": msg, "time": time.time()}
        _refresh_accounts_async()

    activity_log.log(f"[profile] login requested for '{name}'...")
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/profiles/logout", methods=["POST"])
def api_profiles_logout():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    def worker():
        ok, msg = ProfileManager.logout(name)
        activity_log.log(f"[profile] logout '{name}': {msg}")
        _refresh_accounts_async()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


def _ensure_watchdog_running(port=9099):
    """Ensures a Chrome browser watchdog instance is live on debug port 9099 before scraping starts."""
    from wikihow_scraper.pid_tracker.pid_manager import BrowserWatchdog
    profiles = ProfileManager.list_profiles()
    active_profile = profiles[0]["profile_name"] if profiles else "default_profile"
    watchdog = BrowserWatchdog(active_profile, port=port)
    status, _ = watchdog.get_status()
    if status not in ["HEALTHY"]:
        activity_log.log(f"[browser] launching Chrome watchdog for '{active_profile}' on port {port}...")
        try:
            watchdog.should_cleanup = False
            watchdog.launch_browser()
            watchdog._write_tracker("healthy", watchdog._get_tabs() or [])
            activity_log.log(f"[browser] Chrome watchdog ready on port {port}.")
        except Exception as e:
            activity_log.log(f"[browser] Chrome launch warning: {e}")


@app.route("/api/start/single", methods=["POST"])
def api_start_single():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    max_rev = data.get("max_revisions")
    max_rev = int(max_rev) if max_rev else None
    do_update = bool(data.get("update"))

    def on_progress(current, total):
        with cs._progress_lock:
            if title in cs._progress:
                cs._progress[title]["current"] = current
                cs._progress[title]["total"] = total

    def worker():
        cs._stop_event.clear()
        _ensure_watchdog_running(port=9099)
        with cs._progress_lock:
            cs._progress[title] = {"current": 0, "total": max_rev or 0, "start_time": time.time()}
        try:
            if do_update:
                path = update_article_json(title, should_stop=cs._stop_event.is_set)
            else:
                path = scrape_article_to_json(title, max_revisions=max_rev, progress_callback=on_progress,
                                               should_stop=cs._stop_event.is_set)
            activity_log.log(f"[single] '{title}' -> {path}")
        except Exception as e:
            activity_log.log(f"[single] '{title}' FAILED: {e}")
        finally:
            with cs._progress_lock:
                cs._progress.pop(title, None)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/start/batch", methods=["POST"])
def api_start_batch():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")  # "list" or "continuous"
    strategy = data.get("strategy", "sorted")
    param = data.get("param", "")
    sort_type = data.get("sort_type")
    category = data.get("category") or None

    def worker():
        try:
            titles = _resolve_batch_titles(strategy, param, sort_type, category)
            activity_log.log(f"[run] starting {mode} on {len(titles)} article(s)")
            _ensure_watchdog_running(port=9099)
            if mode == "list":
                cs.run_once(titles)
            else:
                cs.add_to_queue(*titles)
                cs.run_continuous(poll_interval=15)
            activity_log.log(f"[run] {mode} finished")
        except Exception as e:
            activity_log.log(f"[run] FAILED: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    cs.stop()
    activity_log.log("[run] stop requested")
    return jsonify({"ok": True})


@app.route("/api/proxy/<action>", methods=["POST"])
def api_proxy(action):
    def worker():
        if action == "connect":
            ok = tor.connect()
            activity_log.log(f"[proxy] connect -> {'ok' if ok else 'failed'}")
        elif action == "rotate":
            ok, msg = tor.rotate_ip()
            activity_log.log(f"[proxy] rotate -> {msg}")
        elif action == "shutdown":
            tor.shutdown()
            activity_log.log("[proxy] shutdown")

    if action not in ("connect", "rotate", "shutdown"):
        return jsonify({"ok": False, "error": "unknown action"}), 400
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/")
def index():
    options_html = "\n".join(
        f'<option value="{slug}">{label}</option>' for label, slug in TOP_LEVEL_CATEGORIES
    )
    html = _INDEX_HTML.replace("__CATEGORY_OPTIONS__", options_html)
    return Response(html, mimetype="text/html")


_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>WikiHow Scraper Dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { background:#111; color:#ddd; font-family: -apple-system, Segoe UI, sans-serif; margin:0; }
  .layout { display:flex; height:100vh; }
  .sidebar { width:320px; padding:16px; border-right:1px solid #333; overflow-y:auto; }
  .main { flex:1; padding:16px; overflow-y:auto; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#888; margin:20px 0 8px; }
  h2:first-child { margin-top:0; }
  button { background:#2a2a2a; color:#ddd; border:1px solid #444; border-radius:4px; padding:8px 10px;
           cursor:pointer; width:100%; margin-bottom:6px; text-align:left; font-size:13px; }
  button:hover { background:#333; }
  button.active { background:#2563eb; border-color:#2563eb; color:#fff; }
  button.danger { background:#7f1d1d; border-color:#991b1b; }
  input { width:100%; box-sizing:border-box; background:#1a1a1a; color:#ddd; border:1px solid #444;
          border-radius:4px; padding:8px; margin-bottom:8px; font-size:13px; }
  .row { display:flex; gap:6px; }
  .row button { margin-bottom:0; }
  .panel { background:#181818; border:1px solid #2a2a2a; border-radius:6px; padding:12px; margin-bottom:16px; }
  .tabbar { display:flex; gap:6px; margin-bottom:16px; }
  .tabbar button { margin-bottom:0; }
  .hidden { display:none; }
  .bar-track { background:#222; border-radius:3px; height:8px; overflow:hidden; margin:4px 0; }
  .bar-fill { background:#2563eb; height:100%; }
  .article-row { margin-bottom:10px; font-size:13px; }
  .muted { color:#888; font-size:12px; }
  .log { font-family: Consolas, monospace; font-size:12px; white-space:pre-wrap; max-height:300px; overflow-y:auto; }
  .status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .status-dot.ok { background:#22c55e; }
  .status-dot.bad { background:#ef4444; }
  .stat-line { font-size:13px; margin:4px 0; }

  #memory_slider_widget { margin-top: 8px; }
  .mem-labels { display:flex; justify-content:space-between; font-size:11px; color:#888; margin-bottom:4px; }
  .mem-track { position:relative; height:22px; background:#151515; border:1px solid #333; border-radius:4px;
               margin-top:18px; margin-bottom:36px; }
  .mem-zone { position:absolute; top:0; bottom:0; }
  .mem-zone.zone-green { background:#166534; z-index:1; }
  .mem-zone.zone-white { background:#e8e8e8; z-index:1; }
  .mem-zone.zone-red { background:#dc2626; z-index:2; }
  .mem-zone.zone-yellow { background:#ca8a04; z-index:2; }
  .mem-handle {
    /* The handle box itself IS the connector line (2px wide, centered on its own
       `left` position via margin-left:-1px) - the triangle is a ::before pseudo
       centered on THIS SAME box using left:50%/translateX(-50%), which shares one
       coordinate system with the line instead of two independently hand-tuned
       pixel offsets (the earlier border-triangle + separately-offset ::after
       version drifted out of alignment - percentage+transform centering can't). */
    position:absolute; top:-14px; width:2px; height:38px; margin-left:-1px;
    background:#2563eb; cursor:ew-resize; z-index:3;
    filter: drop-shadow(0 0 2px rgba(0,0,0,0.6));
  }
  .mem-handle::before {
    content:""; position:absolute; top:0; left:50%; transform:translateX(-50%);
    width:0; height:0;
    border-left: 8px solid transparent; border-right: 8px solid transparent;
    border-top: 12px solid #2563eb;
  }
  .mem-handle:hover, .mem-handle.dragging { background:#60a5fa; }
  .mem-handle:hover::before, .mem-handle.dragging::before { border-top-color:#60a5fa; }
  .mem-handle-label { position:absolute; top:40px; font-size:10px; color:#93c5fd; white-space:nowrap;
                       left:50%; transform:translateX(-50%); }
</style>
</head>
<body>
<div class="layout">
  <div class="sidebar">
    <h2>Mode</h2>
    <button id="mode_continuous" class="active">Continuous</button>
    <button id="mode_single">Single Article</button>
    <button id="mode_list">Batch (Run Once)</button>
    <div class="muted" id="mode_hint" style="margin-bottom:10px;"></div>

    <div id="single_inputs" class="hidden">
      <h2>Single Article</h2>
      <input id="single_title" placeholder="Article title, e.g. Tie-a-Tie">
      <input id="single_max_rev" placeholder="Max revisions (blank = all)">
      <button id="single_update_toggle">Update instead of full scrape</button>
    </div>

    <div id="batch_inputs">
      <h2>Sequencing Strategy</h2>
      <button id="strategy_sorted" class="active">Sorted (site-wide)</button>
      <select id="sort_type_select" style="width:100%;box-sizing:border-box;background:#1a1a1a;color:#ddd;
              border:1px solid #444;border-radius:4px;padding:8px;margin-bottom:8px;font-size:13px;">
        <option value="new">Newest created</option>
        <option value="newest_edits">Newest edits (most recently edited)</option>
        <option value="most_edited">Most edited (highest edit count)</option>
        <option value="fewest_edited">Fewest edited (lowest edit count)</option>
        <option value="oldest">Oldest (least recently touched)</option>
      </select>
      <button id="strategy_random">Random articles</button>
      <button id="strategy_manual">Manual list (comma-separated)</button>
      <input id="param_input" placeholder="Count (blank or 0 = unlimited continuous discovery)">

      <h2>Filter</h2>
      <select id="category_filter_select" style="width:100%;box-sizing:border-box;background:#1a1a1a;color:#ddd;
              border:1px solid #444;border-radius:4px;padding:8px;margin-bottom:4px;font-size:13px;">
        <option value="">No filter (site-wide)</option>
        __CATEGORY_OPTIONS__
      </select>
      <div class="muted" id="filter_hint" style="margin-bottom:8px;"></div>
    </div>

    <h2>Run</h2>
    <div class="row">
      <button id="start_btn" style="background:#166534;border-color:#166534;">Start</button>
      <button id="stop_btn" class="danger">Stop</button>
    </div>
    <div id="run_status" class="muted"></div>

    <h2>Worker Memory Budget</h2>
    <div id="memory_slider_widget">
      <div class="mem-labels">
        <span>0 GB</span>
        <span id="mem_total_label">-- GB total</span>
      </div>
      <div class="mem-track" id="mem_track">
        <div class="mem-zone zone-green" id="mem_zone_green"></div>
        <div class="mem-zone zone-white" id="mem_zone_white"></div>
        <div class="mem-zone zone-red" id="mem_zone_red" style="display:none;"></div>
        <div class="mem-zone zone-yellow" id="mem_zone_yellow"></div>
        <div class="mem-handle" id="mem_handle_min"><div class="mem-handle-label" id="mem_min_label">min</div></div>
        <div class="mem-handle" id="mem_handle_max"><div class="mem-handle-label" id="mem_max_label">max</div></div>
      </div>
      <div class="muted" id="mem_status_line">Used: -- GB / -- GB</div>
    </div>
  </div>

  <div class="main">
    <div class="tabbar">
      <button id="tab_dashboard" class="active">Dashboard</button>
      <button id="tab_accounts">Accounts</button>
      <button id="tab_proxy">Proxy</button>
    </div>

    <div id="page_dashboard">
      <div class="panel">
        <h2 style="margin-top:0;">Queue &amp; Status</h2>
        <div id="queue_stats"></div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0;">Live Progress &amp; Rates</h2>
        <div id="articles"></div>
        <div id="overall_rate" class="stat-line"></div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0;">Activity Log</h2>
        <div id="log" class="log"></div>
      </div>
    </div>

    <div id="page_accounts" class="hidden">
      <div class="panel">
        <h2 style="margin-top:0;">Accounts</h2>
        <button id="refresh_accounts_btn" style="width:auto;">Refresh</button>
        <div id="accounts" style="margin-top:10px;"></div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0;">New Profile</h2>
        <input id="new_profile_name" placeholder="Profile name, e.g. explorer_2">
        <div class="row" style="margin-bottom:8px;">
          <button id="new_mode_anonymous" class="active" style="width:auto;flex:1;">Anonymous</button>
          <button id="new_mode_direct" style="width:auto;flex:1;">WikiHow</button>
          <button id="new_mode_google" style="width:auto;flex:1;">Google</button>
          <button id="new_mode_facebook" style="width:auto;flex:1;">Facebook</button>
        </div>
        <div id="new_profile_creds" class="hidden">
          <input id="new_profile_username" placeholder="Username / email">
          <input id="new_profile_password" type="password" placeholder="Password">
          <div class="muted">Entering credentials auto-logs the profile in right after creation.
          Leave Anonymous selected to create the profile with no credentials - log it in manually later.</div>
        </div>
        <button id="create_profile_btn" style="background:#166534;border-color:#166534;margin-top:8px;">Create Profile</button>
        <div id="create_profile_status" class="muted"></div>
      </div>
    </div>

    <div id="page_proxy" class="hidden">
      <div class="panel">
        <h2 style="margin-top:0;">Tor Proxy</h2>
        <div class="row">
          <button id="proxy_connect">Connect</button>
          <button id="proxy_rotate">Rotate IP</button>
          <button id="proxy_shutdown" class="danger">Shutdown</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let selectedMode = "continuous";
let selectedStrategy = "sorted";

function setActive(ids, activeId) {
  ids.forEach(id => document.getElementById(id).classList.toggle("active", id === activeId));
}

function showTab(tab) {
  ["dashboard", "accounts", "proxy"].forEach(t => {
    document.getElementById("page_" + t).classList.toggle("hidden", t !== tab);
  });
  setActive(["tab_dashboard", "tab_accounts", "tab_proxy"], "tab_" + tab);
}
document.getElementById("tab_dashboard").onclick = () => showTab("dashboard");
document.getElementById("tab_accounts").onclick = () => showTab("accounts");
document.getElementById("tab_proxy").onclick = () => showTab("proxy");

const MODE_HINTS = {
  continuous: "Keeps polling the queue and scraping forever, until you hit Stop.",
  single: "Scrapes exactly one article, once.",
  list: "Scrapes the articles from the strategy below ONCE, then stops (does not keep polling for more).",
};
function setMode(mode) {
  selectedMode = mode;
  setActive(["mode_continuous", "mode_single", "mode_list"], "mode_" + mode);
  document.getElementById("single_inputs").classList.toggle("hidden", mode !== "single");
  document.getElementById("batch_inputs").classList.toggle("hidden", mode === "single");
  document.getElementById("mode_hint").textContent = MODE_HINTS[mode];
}
document.getElementById("mode_continuous").onclick = () => setMode("continuous");
document.getElementById("mode_single").onclick = () => setMode("single");
document.getElementById("mode_list").onclick = () => setMode("list");

const STRATEGY_IDS = ["strategy_sorted", "strategy_random", "strategy_manual"];
const FILTER_HINTS = {
  sorted: "Filter doesn't apply to Sorted - WikiHow's site-wide ranked lists (newest/most-edited/etc.) don't support category scoping.",
  random: "Picks random articles from within this category only (WikiHow's own category-scoped randomizer).",
  manual: "Filter doesn't apply to Manual - you're typing exact titles directly.",
};
function setStrategy(s) {
  selectedStrategy = s;
  setActive(STRATEGY_IDS, "strategy_" + s);
  document.getElementById("sort_type_select").classList.toggle("hidden", s !== "sorted");
  const categorySelect = document.getElementById("category_filter_select");
  const filterApplies = (s === "random");
  categorySelect.disabled = !filterApplies;
  document.getElementById("filter_hint").textContent = FILTER_HINTS[s] || "";
}
document.getElementById("strategy_sorted").onclick = () => setStrategy("sorted");
document.getElementById("strategy_random").onclick = () => setStrategy("random");
document.getElementById("strategy_manual").onclick = () => setStrategy("manual");

let selectedCategory = "";
document.getElementById("category_filter_select").onchange = (e) => { selectedCategory = e.target.value; };

let updateToggle = false;
document.getElementById("single_update_toggle").onclick = (e) => {
  updateToggle = !updateToggle;
  e.target.classList.toggle("active", updateToggle);
};

let newProfileMode = "anonymous";
["anonymous", "direct", "google", "facebook"].forEach(m => {
  document.getElementById("new_mode_" + m).onclick = () => {
    newProfileMode = m;
    setActive(["new_mode_anonymous", "new_mode_direct", "new_mode_google", "new_mode_facebook"], "new_mode_" + m);
    document.getElementById("new_profile_creds").classList.toggle("hidden", m === "anonymous");
  };
});

document.getElementById("create_profile_btn").onclick = async () => {
  const statusEl = document.getElementById("create_profile_status");
  const name = document.getElementById("new_profile_name").value.trim();
  if (!name) { statusEl.textContent = "Enter a profile name."; return; }
  statusEl.textContent = "Creating...";
  const r = await fetch("/api/profiles/create", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      name, mode: newProfileMode,
      username: document.getElementById("new_profile_username").value.trim(),
      password: document.getElementById("new_profile_password").value,
    }),
  });
  const data = await r.json();
  statusEl.textContent = data.ok ? (data.warning || "Profile created.") : ("Error: " + data.error);
  if (data.ok) {
    document.getElementById("new_profile_name").value = "";
    document.getElementById("new_profile_username").value = "";
    document.getElementById("new_profile_password").value = "";
  }
};

async function renameProfile(oldName) {
  const newName = prompt("New name for '" + oldName + "':", oldName);
  if (!newName || newName === oldName) return;
  const r = await fetch("/api/profiles/rename", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({old_name: oldName, new_name: newName}),
  });
  const data = await r.json();
  if (!data.ok) alert(data.message || "Rename failed.");
}

async function deleteProfile(name) {
  if (!confirm("Delete profile '" + name + "'? This removes its saved login session and credentials permanently. If it has a browser currently running, that will be stopped first.")) return;
  const r = await fetch("/api/profiles/delete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name}),
  });
  const data = await r.json();
  if (!data.ok) alert(data.message || "Delete failed.");
}

function loginProfile(name) {
  fetch("/api/profiles/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name}),
  });
}

function logoutProfile(name) {
  fetch("/api/profiles/logout", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name}),
  });
}

// --- Worker Memory Budget slider ---
// Track fills with GREY from the left representing memory CURRENTLY IN USE
// (system-wide, live) - it grows/shrinks in real time as usage changes, and since
// usage is "the rest of the bar minus free space", and free space sits on the
// right, the used/free boundary effectively marks where "occupied" reaches FROM
// THE RIGHT. Two draggable handles (min/max, in GB) define the worker memory
// budget band:
//   - occupied boundary is PAST (left of) MIN  -> [0, MIN] turns RED: even the
//     minimum reserved headroom is gone, something needs to free memory.
//   - otherwise                                 -> [0, occupied boundary] stays
//     GREY (normal usage, not a concern), and [occupied boundary, MAX] is YELLOW
//     (the remaining safe budget still available for workers within the band).
// Positions persist server-side (settings.json) so they survive across sessions.
let memMinFrac = 0.35, memMaxFrac = 0.75;
let lastUsedGb = 0, lastTotalGb = 16;
let memRangeLoaded = false;

async function loadMemoryRange() {
  try {
    const r = await fetch("/api/settings/memory_range");
    const data = await r.json();
    if (data.min_gb != null && data.max_gb != null && lastTotalGb > 0) {
      memMinFrac = data.min_gb / lastTotalGb;
      memMaxFrac = data.max_gb / lastTotalGb;
    }
  } catch (e) { /* keep defaults */ }
  memRangeLoaded = true;
}

async function saveMemoryRange() {
  await fetch("/api/settings/memory_range", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      min_gb: Math.round(memMinFrac * lastTotalGb * 10) / 10,
      max_gb: Math.round(memMaxFrac * lastTotalGb * 10) / 10,
    }),
  });
}

function renderMemoryTrack() {
  const track = document.getElementById("mem_track");
  const trackWidth = track.clientWidth || 280;
  const totalGb = lastTotalGb, usedGb = lastUsedGb;

  document.getElementById("mem_total_label").textContent = totalGb.toFixed(1) + " GB total";

  const minPx = memMinFrac * trackWidth;
  const maxPx = memMaxFrac * trackWidth;
  const occupiedBoundaryFrac = totalGb > 0 ? (1 - usedGb / totalGb) : 1;
  const occupiedBoundaryPx = Math.max(0, Math.min(trackWidth, occupiedBoundaryFrac * trackWidth));

  const minHandle = document.getElementById("mem_handle_min");
  const maxHandle = document.getElementById("mem_handle_max");
  minHandle.style.left = minPx + "px";
  maxHandle.style.left = maxPx + "px";
  // Just "min"/"max" on the label itself (numeric GB values there collided with the
  // handle's connector line) - exact value is still available on hover via title.
  const minGb = (memMinFrac * totalGb).toFixed(1);
  const maxGb = (memMaxFrac * totalGb).toFixed(1);
  document.getElementById("mem_min_label").textContent = "min";
  document.getElementById("mem_max_label").textContent = "max";
  minHandle.title = minGb + " GB";
  maxHandle.title = maxGb + " GB";
  document.getElementById("mem_status_line").textContent =
    "Used: " + usedGb.toFixed(1) + " / " + totalGb.toFixed(1) + " GB  |  min: " + minGb + " GB, max: " + maxGb + " GB";

  const zoneGreen = document.getElementById("mem_zone_green");
  const zoneWhite = document.getElementById("mem_zone_white");
  const zoneRed = document.getElementById("mem_zone_red");
  const zoneYellow = document.getElementById("mem_zone_yellow");

  // Base layer, always real-time and unconditional: GREEN = free memory [0,
  // occupiedBoundaryPx], WHITE = used memory [occupiedBoundaryPx, trackWidth] -
  // used fills from the RIGHT edge leftward as usage grows.
  zoneGreen.style.left = "0px";
  zoneGreen.style.width = occupiedBoundaryPx + "px";
  zoneWhite.style.left = occupiedBoundaryPx + "px";
  zoneWhite.style.width = Math.max(0, trackWidth - occupiedBoundaryPx) + "px";

  // Overlay on top of the base layer: the [min,max] band is always highlighted
  // YELLOW as the configured target reserve, regardless of whether that span is
  // currently free or used. If usage has crept PAST min (a breach), the intruded
  // slice [occupiedBoundaryPx, min] is highlighted RED instead of plain white.
  if (occupiedBoundaryPx <= minPx) {
    zoneRed.style.display = "block";
    zoneRed.style.left = occupiedBoundaryPx + "px";
    zoneRed.style.width = Math.max(0, minPx - occupiedBoundaryPx) + "px";
  } else {
    zoneRed.style.display = "none";
  }
  zoneYellow.style.left = minPx + "px";
  zoneYellow.style.width = Math.max(0, maxPx - minPx) + "px";
}

function updateMemoryStats(usedGb, totalGb) {
  lastUsedGb = usedGb;
  lastTotalGb = totalGb;
  renderMemoryTrack();
}

function setupMemoryHandleDrag(handleId, isMin) {
  const handle = document.getElementById(handleId);
  handle.addEventListener("pointerdown", (e) => {
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    const track = document.getElementById("mem_track");

    const onMove = (moveEvent) => {
      const rect = track.getBoundingClientRect();
      let frac = (moveEvent.clientX - rect.left) / rect.width;
      frac = Math.max(0, Math.min(1, frac));
      if (isMin) {
        memMinFrac = Math.min(frac, memMaxFrac);
      } else {
        memMaxFrac = Math.max(frac, memMinFrac);
      }
      renderMemoryTrack();
    };
    const onUp = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      saveMemoryRange();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  });
}
setupMemoryHandleDrag("mem_handle_min", true);
setupMemoryHandleDrag("mem_handle_max", false);

document.getElementById("start_btn").onclick = async () => {
  const statusEl = document.getElementById("run_status");
  if (selectedMode === "single") {
    const title = document.getElementById("single_title").value.trim();
    if (!title) { statusEl.textContent = "Enter an article title."; return; }
    statusEl.textContent = "Starting...";
    await fetch("/api/start/single", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        title, max_revisions: document.getElementById("single_max_rev").value.trim(),
        update: updateToggle,
      }),
    });
  } else {
    statusEl.textContent = "Starting...";
    await fetch("/api/start/batch", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        mode: selectedMode, strategy: selectedStrategy,
        param: document.getElementById("param_input").value.trim(),
        sort_type: document.getElementById("sort_type_select").value,
        category: selectedCategory,
      }),
    });
  }
  statusEl.textContent = "Running.";
};

document.getElementById("stop_btn").onclick = async () => {
  await fetch("/api/stop", {method: "POST"});
  document.getElementById("run_status").textContent = "Stop requested...";
};

document.getElementById("refresh_accounts_btn").onclick = () => fetch("/api/accounts/refresh", {method: "POST"});
document.getElementById("proxy_connect").onclick = () => fetch("/api/proxy/connect", {method: "POST"});
document.getElementById("proxy_rotate").onclick = () => fetch("/api/proxy/rotate", {method: "POST"});
document.getElementById("proxy_shutdown").onclick = () => fetch("/api/proxy/shutdown", {method: "POST"});

async function poll() {
  try {
    const r = await fetch("/api/status");
    const data = await r.json();

    document.getElementById("queue_stats").innerHTML = `
      <div class="stat-line"><span class="status-dot ${data.running ? 'ok' : 'bad'}"></span>Run state: ${data.running ? 'RUNNING' : 'stopped'}</div>
      <div class="stat-line">Queue: pending=${data.queue.pending} completed=${data.queue.completed} failed=${data.queue.failed}</div>
      <div class="stat-line"><span class="status-dot ${data.tor.status === 'ONLINE' ? 'ok' : 'bad'}"></span>Tor: ${data.tor.status} | IP: ${data.tor.current_ip || '-'}</div>
    `;

    const articlesEl = document.getElementById("articles");
    if (data.articles.length === 0) {
      articlesEl.innerHTML = '<div class="muted">(nothing actively scraping)</div>';
    } else {
      articlesEl.innerHTML = data.articles.map(a => {
        const pct = a.total ? Math.round(100 * a.current / a.total) : 0;
        return `<div class="article-row">
          <div>${a.title} - ${a.current}/${a.total} (${pct}%) - ${a.rate.toFixed(2)} rev/s</div>
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
        </div>`;
      }).join("");
    }
    document.getElementById("overall_rate").textContent = `Overall rate: ${data.overall_rate.toFixed(2)} rev/s across ${data.articles.length} article(s)`;

    const accountsEl = document.getElementById("accounts");
    if (data.accounts.length === 0) {
      accountsEl.innerHTML = '<div class="muted">No profiles configured, or not checked yet - click Refresh.</div>';
    } else {
      accountsEl.innerHTML = data.accounts.map(a => {
        const ok = a.status === "logged_in";
        const statusText = ok ? 'LOGGED IN' : (a.status === 'error' ? 'ERROR: ' + a.detail : 'LOGGED OUT');
        const nameEsc = a.name.replace(/'/g, "\\'");
        // last_login is null once a success is fully reflected by LOGGED IN above (see
        // webui.py's accounts-refresh worker) - so if it's present here, it's either a
        // failure (wrong password/username, timeout) or a success still catching up to
        // the next status check. Surfaces "why" a login attempt didn't just work.
        const ll = a.last_login;
        const llLine = ll
          ? `<div class="stat-line" style="margin:2px 0 0 0;font-size:0.85em;color:${ll.ok ? '#4ade80' : '#f87171'};">
               ${ll.ok ? 'Last attempt: success - ' : 'Last attempt FAILED: '}${(ll.message || '').replace(/</g, '&lt;')}
             </div>`
          : '';
        return `<div class="account-row" style="display:flex;align-items:center;justify-content:space-between;
                     padding:8px 0;border-bottom:1px solid #2a2a2a;">
          <div class="stat-line" style="margin:0;flex-direction:column;align-items:flex-start;">
            <div><span class="status-dot ${ok ? 'ok' : 'bad'}"></span><b>${a.name}</b> | method: ${a.method} | ${statusText}</div>
            ${llLine}
          </div>
          <div class="row" style="width:auto;gap:4px;">
            <button style="width:auto;" onclick="renameProfile('${nameEsc}')" title="Rename">Rename</button>
            ${ok
              ? `<button style="width:auto;" onclick="logoutProfile('${nameEsc}')" title="Log out">Logout</button>`
              : `<button style="width:auto;" onclick="loginProfile('${nameEsc}')" title="Log in">Login</button>`
            }
            <button style="width:auto;background:#7f1d1d;border-color:#991b1b;" onclick="deleteProfile('${nameEsc}')" title="Delete">Delete</button>
          </div>
        </div>`;
      }).join("");
    }

    const logEl = document.getElementById("log");
    const wasAtBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 10;
    logEl.textContent = data.log.map(l => `[${new Date(l.time * 1000).toLocaleTimeString()}] ${l.message}`).join("\\n");
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;

    if (data.memory) {
      if (!memRangeLoaded) {
        lastTotalGb = data.memory.total_gb;  // needed before loadMemoryRange() can convert GB -> fraction
        await loadMemoryRange();
      }
      updateMemoryStats(data.memory.used_gb, data.memory.total_gb);
    }
  } catch (e) {
    console.error(e);
  }
}
setMode("continuous");
setStrategy("sorted");
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


def launch_webui(host="127.0.0.1", port=8899, debug=False):
    _refresh_accounts_async()
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    launch_webui()
