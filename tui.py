"""
Interactive TUI dashboard (textual-based) - the one entry point for everything the
CLI can also do one-off, plus live metrics a one-shot command can't show.

Layout:
  LEFT sidebar (always visible): pick a mode - Continuous / Single Article / List of
  Articles - configure it, then Start/Stop (also bound to 's'/'x').
  RIGHT panel (tab-switched): Dashboard (active articles + per-article and overall
  scrape rate, queue counts, Tor status, activity log), Accounts (every profile's
  live login status + account name + login method), Proxy (Tor controls).

Continuous/List modes are menu-driven: pick a SEQUENCING STRATEGY (random, recently
edited site-wide, articles in a category, or a manual list) rather than being
required to type exact article titles - see discovery.py for how each strategy
actually sources its article list.

Architecture notes (all confirmed via extensive headless run_test() debugging before
landing on this design - a reproducible compositor crash, 'NoneType has no attribute
get_height', traced through _arrange_root/resolve_box_models):
  1. Single Screen with content panels toggled by visibility, not multiple pushed
     Screen instances - push_screen/pop_screen's stack machinery was implicated in
     early crash traces (_compositor.reflow, focus_chain, _prune).
  2. No RadioSet/RadioButton or Log widgets - both were still implicated in crashes
     even after eliminating the Screen stack. Only Button/Static/Input/Label proved
     reliable. "Radio" selection is plain toggle Button widgets; the activity/log
     panels are Static with an internal string buffer, not the Log widget class.
  3. NEVER name a widget method _render() or refresh() - both shadow Textual's own
     internal Widget methods of the same name, silently breaking the layout engine
     (this was the actual root cause of the crash above - not the two points above,
     though both are still worth keeping as they were real, separately-useful fixes).
  4. A page/container must complete ITS OWN first layout pass before being hidden -
     toggle initial visibility via call_after_refresh() in on_mount(), not synchronously.
"""

import time
import threading

from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.widgets import Header, Footer, Static, Button, Input, Label
from textual.reactive import reactive

from wikihow_scraper.proxy import tor
from wikihow_scraper.profiles import ProfileManager
from wikihow_scraper.article_pipeline import scrape_article_to_json, update_article_json
from wikihow_scraper import continuous_scraper as cs
from wikihow_scraper import activity_log as shared_activity_log
from wikihow_scraper.discovery import random_articles, recent_changes, category_articles


class AccountsPanel(Static):
    """Live login status + account name + login method for every configured
    profile. Refreshed explicitly (on mount, on 'r', on tab switch) via a Textual
    worker thread - see module docstring point 3 for why this isn't named _render()."""

    lines = reactive([])

    def refresh_accounts(self):
        self.run_worker(self._check_accounts, thread=True, exclusive=True)

    def _check_accounts(self):
        rows = []
        profiles = ProfileManager.list_profiles()
        if not profiles:
            rows.append("No profiles configured. (cli: profile add <name>)")
        for p in profiles:
            name = p["profile_name"]
            method = p.get("provider", "Unknown")
            try:
                is_logged_in, _ = ProfileManager.check_login_status(name, auto_prompt_login=False)
                state = "[green]LOGGED IN[/green]" if is_logged_in else "[red]LOGGED OUT[/red]"
            except Exception as e:
                state = f"[red]ERROR: {e}[/red]"
            rows.append(f"[b]{name}[/b]  |  method: {method}  |  {state}")
        if self.is_mounted:
            self.lines = rows
            self._apply_content()

    def _apply_content(self):
        self.update("\n".join(self.lines) if self.lines else "(checking accounts...)")


class RatesPanel(Static):
    """Active articles with per-article revisions/sec, plus the aggregate rate across
    everything currently scraping. Refreshed explicitly, same pattern as AccountsPanel."""

    def refresh_rates(self):
        progress = cs.get_progress()
        if not progress:
            self.update("(nothing actively scraping)")
            return

        now = time.time()
        lines = []
        for title, info in progress.items():
            current, total = info["current"], info["total"]
            elapsed = max(now - info["start_time"], 0.01)
            rate = current / elapsed
            pct = int(100 * current / total) if total else 0
            filled = int(24 * current / total) if total else 0
            bar = "#" * filled + "-" * (24 - filled)
            lines.append(f"{title:<28} [{bar}] {current}/{total} ({pct}%)  {rate:.2f} rev/s")

        overall = cs.get_overall_rate()
        lines.append("")
        lines.append(f"[b]Overall rate:[/b] {overall:.2f} rev/s across {len(progress)} article(s)")
        self.update("\n".join(lines))


class QueuePanel(Static):
    """Queue pending/completed/failed counts + Tor status - refreshed explicitly."""

    def refresh_queue(self):
        q = cs.get_queue_status()
        info = tor.get_status()
        state = "RUNNING" if cs.is_running() else "stopped"
        self.update(
            f"[b]Run state:[/b] {state}\n"
            f"[b]Queue:[/b] pending={q['pending']}  completed={q['completed']}  failed={q['failed']}\n"
            f"[b]Tor:[/b] {info['status']} | IP: {info['current_ip'] or '-'}"
        )


class LogPanel(Static):
    """Shared activity log - job results get written here from any panel. A Static
    with an internal string buffer, not the Log widget class (see module docstring)."""

    MAX_LINES = 200

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lines = ["(activity log)"]

    def on_mount(self):
        self.update("\n".join(self._lines))

    def write_line(self, message):
        self._lines.append(message)
        self._lines = self._lines[-self.MAX_LINES:]
        self.update("\n".join(self._lines))


_activity_log = None  # set once the app mounts; simple global so any panel can log


def log_activity(message):
    shared_activity_log.log(message)
    if _activity_log is not None:
        _activity_log.write_line(message)


class WikiHowScraperApp(App):
    CSS = """
    .title { text-style: bold; margin-top: 1; }
    #left_sidebar { width: 32; padding: 1; border-right: solid $primary; }
    #right_panel { padding: 1; }
    Button { margin-bottom: 1; width: 100%; }
    .tab-page { padding: 1; }
    #mode_buttons Button, #tab_buttons Button { width: 1fr; }
    #tab_buttons { height: 3; }
    #start_stop Button { width: 1fr; }
    """

    BINDINGS = [
        ("r", "refresh_dashboard", "Refresh"),
        ("s", "start_run", "Start"),
        ("x", "stop_run", "Stop"),
    ]

    _selected_mode = "mode_continuous"
    _MODE_IDS = ("mode_continuous", "mode_single", "mode_list")
    _selected_strategy = "strategy_random"
    _STRATEGY_IDS = ("strategy_random", "strategy_recent", "strategy_category", "strategy_manual")
    _selected_tab = "tab_dashboard"
    _TAB_IDS = ("tab_dashboard", "tab_accounts", "tab_proxy")

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            # ---------------- LEFT: persistent control sidebar ----------------
            with Vertical(id="left_sidebar"):
                yield Label("MODE", classes="title")
                with Vertical(id="mode_buttons"):
                    yield Button("> Continuous", id="mode_continuous", variant="primary")
                    yield Button("  Single Article", id="mode_single")
                    yield Button("  List of Articles", id="mode_list")

                with Vertical(id="single_inputs"):
                    yield Label("Article title:")
                    yield Input(placeholder="e.g. Tie-a-Tie", id="single_title_input")
                    yield Input(placeholder="Max revisions (blank = all)", id="single_max_rev_input")
                    yield Button("Update instead of full scrape", id="single_update_toggle")

                with Vertical(id="batch_inputs"):
                    yield Label("Sequencing strategy:")
                    yield Button("> Random articles", id="strategy_random", variant="primary")
                    yield Button("  Recently edited (site-wide)", id="strategy_recent")
                    yield Button("  Articles in a category", id="strategy_category")
                    yield Button("  Manual list (comma-separated)", id="strategy_manual")
                    yield Label("Count / category name / titles:")
                    yield Input(placeholder="e.g. 10  |  Ties  |  Tie-a-Tie,Bake-a-Cake", id="param_input")

                with Horizontal(id="start_stop"):
                    yield Button("Start (s)", id="start_btn", variant="success")
                    yield Button("Stop (x)", id="stop_btn", variant="error")
                yield Label(" ", id="run_status")

            # ---------------- RIGHT: tab-switched panels ----------------
            with Vertical(id="right_panel"):
                with Horizontal(id="tab_buttons"):
                    yield Button("Dashboard", id="tab_dashboard", variant="primary")
                    yield Button("Accounts", id="tab_accounts")
                    yield Button("Proxy", id="tab_proxy")

                with VerticalScroll(id="page_tab_dashboard", classes="tab-page"):
                    yield Label("QUEUE & PROXY", classes="title")
                    yield QueuePanel(id="queue_panel")
                    yield Label("LIVE PROGRESS & RATES", classes="title")
                    yield RatesPanel(id="rates_panel")
                    yield Label("ACTIVITY LOG", classes="title")
                    yield LogPanel(id="activity_log")

                with VerticalScroll(id="page_tab_accounts", classes="tab-page"):
                    yield Label("ACCOUNTS (press 'r' to refresh)", classes="title")
                    yield AccountsPanel(id="accounts_panel")

                with VerticalScroll(id="page_tab_proxy", classes="tab-page"):
                    yield Label("Tor Proxy", classes="title")
                    with Horizontal():
                        yield Button("Connect", id="connect_btn")
                        yield Button("Rotate IP", id="rotate_btn")
                        yield Button("Shutdown", id="shutdown_btn")
                    yield Label(" ", id="proxy_status")
        yield Footer()

    def on_mount(self):
        global _activity_log
        _activity_log = self.query_one("#activity_log", LogPanel)
        # Deferred: see module docstring point 4.
        self.call_after_refresh(self._apply_initial_visibility)
        self.action_refresh_dashboard()
        self.set_interval(2, self._tick)

    def _apply_initial_visibility(self):
        self._show_tab("tab_dashboard")
        self._update_mode_inputs()

    def _tick(self):
        # Lightweight periodic refresh ONLY for the numbers that change during an
        # active run (rates/queue) - safe because these panels are simple Statics
        # updated in place, never display-toggled by this timer (see docstring
        # points 3-4; the earlier crash was about toggling display on widgets that
        # hadn't been laid out, not about updating already-visible widget content).
        if self._selected_tab == "tab_dashboard":
            self.query_one("#rates_panel", RatesPanel).refresh_rates()
            self.query_one("#queue_panel", QueuePanel).refresh_queue()

    # --- Tab / mode / strategy selection (all the same proven toggle-button pattern) ---

    def _show_tab(self, tab_id):
        self._selected_tab = tab_id
        for page in self.query(".tab-page"):
            page.display = (page.id == f"page_{tab_id}")
        for btn in self.query("#tab_buttons Button"):
            btn.variant = "primary" if btn.id == tab_id else "default"

    def _select_mode(self, mode_id):
        self._selected_mode = mode_id
        for btn in self.query("#mode_buttons Button"):
            is_selected = btn.id == mode_id
            btn.variant = "primary" if is_selected else "default"
            label = str(btn.label).lstrip("> ").strip()
            btn.label = f"> {label}" if is_selected else f"  {label}"
        self._update_mode_inputs()

    def _update_mode_inputs(self):
        self.query_one("#single_inputs").display = (self._selected_mode == "mode_single")
        self.query_one("#batch_inputs").display = (self._selected_mode in ("mode_continuous", "mode_list"))

    def _select_strategy(self, strategy_id):
        self._selected_strategy = strategy_id
        for btn in self.query("#batch_inputs Button"):
            if btn.id not in self._STRATEGY_IDS:
                continue
            is_selected = btn.id == strategy_id
            btn.variant = "primary" if is_selected else "default"
            label = str(btn.label).lstrip("> ").strip()
            btn.label = f"> {label}" if is_selected else f"  {label}"

    def action_refresh_dashboard(self):
        self.query_one("#queue_panel", QueuePanel).refresh_queue()
        self.query_one("#rates_panel", RatesPanel).refresh_rates()
        self.query_one("#accounts_panel", AccountsPanel).refresh_accounts()

    # --- Start / Stop ---

    def action_start_run(self):
        self._do_start()

    def action_stop_run(self):
        cs.stop()
        log_activity("[run] stop requested")
        self.query_one("#run_status", Label).update("Stop requested...")

    def _resolve_batch_titles(self):
        param = self.query_one("#param_input", Input).value.strip()
        strategy = self._selected_strategy

        if strategy == "strategy_random":
            n = int(param) if param.isdigit() else 10
            return random_articles(n)
        elif strategy == "strategy_recent":
            n = int(param) if param.isdigit() else 20
            return recent_changes(n)
        elif strategy == "strategy_category":
            if not param:
                raise ValueError("Enter a category name (e.g. 'Ties') in the input box.")
            return category_articles(param)
        else:  # manual
            titles = [t.strip() for t in param.split(",") if t.strip()]
            if not titles:
                raise ValueError("Enter comma-separated article titles in the input box.")
            return titles

    def _do_start(self):
        status = self.query_one("#run_status", Label)
        mode = self._selected_mode

        if mode == "mode_single":
            title = self.query_one("#single_title_input", Input).value.strip()
            if not title:
                status.update("[red]Enter an article title.[/red]")
                return
            max_rev_raw = self.query_one("#single_max_rev_input", Input).value.strip()
            max_rev = int(max_rev_raw) if max_rev_raw.isdigit() else None
            do_update = self.query_one("#single_update_toggle", Button).variant == "primary"
            status.update(f"{'Updating' if do_update else 'Scraping'} '{title}'...")

            def worker():
                try:
                    if do_update:
                        path = update_article_json(title)
                    else:
                        path = scrape_article_to_json(title, max_revisions=max_rev)
                    self.call_from_thread(status.update, f"[green]Done: {path}[/green]")
                    log_activity(f"[single] '{title}' -> {path}")
                except Exception as e:
                    self.call_from_thread(status.update, f"[red]Failed: {e}[/red]")
                    log_activity(f"[single] '{title}' FAILED: {e}")

            threading.Thread(target=worker, daemon=True).start()
            return

        # mode_continuous or mode_list
        status.update("Resolving article list...")

        def worker():
            try:
                titles = self._resolve_batch_titles()
                self.call_from_thread(status.update, f"Starting on {len(titles)} article(s)...")
                log_activity(f"[run] starting {mode} on {len(titles)} article(s)")
                if mode == "mode_list":
                    cs.run_once(titles)
                else:
                    cs.add_to_queue(*titles)
                    cs.run_continuous(poll_interval=15)
                self.call_from_thread(status.update, "Done." if mode == "mode_list" else "Stopped.")
                log_activity(f"[run] {mode} finished")
            except Exception as e:
                self.call_from_thread(status.update, f"[red]Error: {e}[/red]")
                log_activity(f"[run] FAILED: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # --- Button dispatch ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id

        if bid in self._TAB_IDS:
            self._show_tab(bid)
            if bid == "tab_accounts":
                self.query_one("#accounts_panel", AccountsPanel).refresh_accounts()
        elif bid in self._MODE_IDS:
            self._select_mode(bid)
        elif bid in self._STRATEGY_IDS:
            self._select_strategy(bid)
        elif bid == "single_update_toggle":
            btn = event.button
            btn.variant = "default" if btn.variant == "primary" else "primary"
        elif bid == "start_btn":
            self._do_start()
        elif bid == "stop_btn":
            self.action_stop_run()
        elif bid in ("connect_btn", "rotate_btn", "shutdown_btn"):
            self._do_proxy_action(bid)

    def _do_proxy_action(self, button_id):
        status = self.query_one("#proxy_status", Label)

        def worker():
            if button_id == "connect_btn":
                ok = tor.connect()
                self.call_from_thread(status.update, "Connected." if ok else "Connect failed.")
            elif button_id == "rotate_btn":
                ok, msg = tor.rotate_ip()
                self.call_from_thread(status.update, msg)
            elif button_id == "shutdown_btn":
                tor.shutdown()
                self.call_from_thread(status.update, "Tor shut down.")
            log_activity(f"[proxy] {button_id}")

        threading.Thread(target=worker, daemon=True).start()


def launch_tui():
    WikiHowScraperApp().run()


if __name__ == "__main__":
    launch_tui()
