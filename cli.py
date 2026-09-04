import argparse
import sys
from wikihow_scraper.proxy import tor
from wikihow_scraper.profiles import ProfileManager
from wikihow_scraper.pid_tracker.pid_manager import BrowserWatchdog
from wikihow_scraper.article_pipeline import scrape_article_to_json, update_article_json
from wikihow_scraper.continuous_scraper import add_to_queue, run_continuous
from wikihow_scraper.tui import launch_tui
from wikihow_scraper.webui import launch_webui

def handle_proxy(args):
    if args.action == "connect":
        tor.connect()
    elif args.action == "status":
        status = tor.get_status()
        print(f"Tor Status: {status['status']}")
        print(f"SOCKS Port: {status['proxy_port']}")
        print(f"Control Port: {status['control_port']}")
        print(f"IP: {status['current_ip']}")
    elif args.action == "rotate":
        success, msg = tor.rotate_ip()
        print(msg)
    elif args.action == "shutdown":
        tor.shutdown()
    else:
        print("Unknown action. Use: connect, status, rotate, shutdown")

def handle_profile(args):
    if args.action == "tui":
        ProfileManager.interactive_menu()
    elif args.action == "add":
        if not args.name:
            print("Error: --name is required to add a profile.")
            return
        success, msg = ProfileManager.add_profile(args.name)
        print(msg)
    elif args.action == "list":
        profiles = ProfileManager.list_profiles()
        print("Existing Profiles & Connected Accounts:")
        for p in profiles:
            print(f" - Profile: {p['profile_name']} | Provider: {p['provider']} | Account: {p['account_name']}")
    elif args.action == "delete":
        if not args.name:
            print("Error: --name is required to delete a profile.")
            return
        success, msg = ProfileManager.delete_profile(args.name)
        print(msg)
    elif args.action == "login":
        if not args.name:
            print("Error: --name is required to log in to a profile.")
            return
        success, msg = ProfileManager.login(args.name, prefer_manual=args.manual)
        print(msg)
    elif args.action == "check-login":
        if not args.name:
            print("Error: --name is required to check login status.")
            return
        status, msg = ProfileManager.check_login_status(args.name, auto_prompt_login=args.login_if_needed)
        print(msg)
    elif args.action == "set-credentials":
        if not args.name:
            print("Error: --name is required to set credentials.")
            return
        if not args.username or not args.password:
            print("Error: --username and --password are both required for set-credentials.")
            return
        cred_file = ProfileManager.save_credentials(args.name, args.username, args.password, mode=args.mode)
        print(f"Saved '{args.mode}' credentials for '{args.name}' to {cred_file}")
        print("Note: stored as plaintext JSON, local to this machine only. `profile login` will now auto-login by default.")
    else:
        print("Unknown action. Use: tui, add, list, delete, login, check-login, set-credentials")

def handle_browser(args):
    watchdog = BrowserWatchdog(args.profile_name, port=args.port)
    if args.action == "start":
        watchdog.start_watchdog()
    elif args.action == "status":
        status, info = watchdog.get_status()
        print(f"Browser Watchdog Status: {status}")
        if info:
            print(f"  Port: {info.get('port')}")
            print(f"  PID: {info.get('chrome_pid')}")
            print(f"  Profile Path: {info.get('user_data_dir')}")
            print(f"  Open Tabs: {len(info.get('tabs', []))}")
    elif args.action == "stop":
        watchdog.stop()

def handle_revisions(args):
    path = scrape_article_to_json(
        args.target, port=args.port, max_revisions=args.max_revisions,
        include_snapshots=not args.no_snapshots,
    )
    print(f"Saved: {path}")

def handle_update(args):
    path = update_article_json(
        args.target, port=args.port,
        fetch_missing_snapshots=not args.no_backfill,
        max_new_snapshots=args.max_new_snapshots,
    )
    print(f"Updated: {path}")

def handle_queue(args):
    if args.action == "add":
        titles = [t.strip() for t in args.titles.split(",") if t.strip()]
        add_to_queue(*titles)
        print(f"Added {len(titles)} title(s) to the queue: {titles}")
    elif args.action == "run":
        run_continuous(
            port=args.port, max_workers=args.workers, max_revisions=args.max_revisions,
            poll_interval=args.poll_interval, run_seconds=args.run_seconds,
        )
    else:
        print("Unknown action. Use: add, run")

def main():
    # If run without arguments, launch TUI dashboard by default
    if len(sys.argv) == 1:
        launch_tui()
        return

    parser = argparse.ArgumentParser(description="WikiHow Standalone Scraper CLI & TUI Dashboard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # TUI Dashboard parser
    subparsers.add_parser("tui", help="Launch live interactive Scraper Dashboard TUI")

    # Proxy parser
    proxy_parser = subparsers.add_parser("proxy", help="Manage Tor SOCKS proxy")
    proxy_parser.add_argument("action", choices=["connect", "status", "rotate", "shutdown"])

    # Profile parser
    profile_parser = subparsers.add_parser("profile", help="Manage Chrome browser profiles and authenticated accounts")
    profile_parser.add_argument("action", choices=["tui", "add", "list", "delete", "login", "check-login", "set-credentials"])
    profile_parser.add_argument("--name", help="Profile name")
    profile_parser.add_argument("--login-if-needed", action="store_true",
                                 help="For check-login: if not logged in, immediately open the login flow "
                                      "instead of just reporting the status (off by default).")
    profile_parser.add_argument("--manual", action="store_true",
                                 help="For login: force the manual (human-driven) login page even if "
                                      "saved credentials exist for this profile.")
    profile_parser.add_argument("--username", help="For set-credentials: username/email for the chosen mode")
    profile_parser.add_argument("--password", help="For set-credentials: password for the chosen mode")
    profile_parser.add_argument("--mode", choices=["direct", "google", "facebook"], default="direct",
                                 help="For set-credentials: which login flow these credentials are for "
                                      "(default: direct WikiHow login).")

    # Browser parser
    browser_parser = subparsers.add_parser("browser", help="Manage self-healing browser instances")
    browser_parser.add_argument("action", choices=["start", "status", "stop"])
    browser_parser.add_argument("profile_name", help="Name of browser profile to launch")
    browser_parser.add_argument("--port", type=int, default=9099, help="CDP debug port (default: 9099)")

    # Revisions parser (one-shot full article scrape)
    rev_parser = subparsers.add_parser("revisions", help="Scrape an article's full revision history + wikitext to data/articles/")
    rev_parser.add_argument("target", help="Article title, e.g. Tie-a-Tie")
    rev_parser.add_argument("--port", type=int, default=9099, help="CDP debug port of the attached watchdog browser")
    rev_parser.add_argument("--max-revisions", type=int, default=None,
                             help="Cap on how many of the newest revisions get a wikitext snapshot (default: all)")
    rev_parser.add_argument("--no-snapshots", action="store_true", help="Metadata only - skip wikitext fetching entirely")

    # Update parser (incremental re-scrape)
    update_parser = subparsers.add_parser("update", help="Incrementally update an already-scraped article (new revisions + missing snapshots only)")
    update_parser.add_argument("target", help="Article title, e.g. Tie-a-Tie")
    update_parser.add_argument("--port", type=int, default=9099)
    update_parser.add_argument("--max-new-snapshots", type=int, default=None,
                                help="Cap on how many NEW revisions get a snapshot this run (default: all new ones)")
    update_parser.add_argument("--no-backfill", action="store_true",
                                help="Don't backfill snapshot_wikitext for existing revisions that are missing one")

    # Queue parser (continuous scraper)
    queue_parser = subparsers.add_parser("queue", help="Manage the continuous scraping queue")
    queue_parser.add_argument("action", choices=["add", "run"])
    queue_parser.add_argument("titles", nargs="?", help="For 'add': comma-separated article titles")
    queue_parser.add_argument("--port", type=int, default=9099)
    queue_parser.add_argument("--workers", type=int, default=None, help="Concurrent articles per batch (default: RAM-adaptive)")
    queue_parser.add_argument("--max-revisions", type=int, default=None, help="Snapshot cap per article (default: all)")
    queue_parser.add_argument("--poll-interval", type=int, default=15, help="Seconds to sleep when the queue is empty")
    queue_parser.add_argument("--run-seconds", type=int, default=None, help="Stop after this many seconds (default: run forever)")

    # Web UI parser
    webui_parser = subparsers.add_parser("webui", help="Launch the web dashboard (same capabilities as the TUI)")
    webui_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    webui_parser.add_argument("--port", type=int, default=8899, help="Bind port (default: 8899)")

    args = parser.parse_args()

    if args.command == "tui":
        launch_tui()
    elif args.command == "proxy":
        handle_proxy(args)
    elif args.command == "profile":
        handle_profile(args)
    elif args.command == "browser":
        handle_browser(args)
    elif args.command == "revisions":
        handle_revisions(args)
    elif args.command == "update":
        handle_update(args)
    elif args.command == "queue":
        handle_queue(args)
    elif args.command == "webui":
        launch_webui(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
