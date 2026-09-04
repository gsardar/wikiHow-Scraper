"""
End-to-end per-article scraping: fetches full revision history + one wikitext
snapshot per revision + top-level category, and writes ONE JSON file per article to

    data/articles/<Top-Level-Category>/<Article-Title>.json

Design (per discussion): each revision record stores only ITS OWN final wikitext
snapshot - not a duplicate "parent" snapshot (the previous revision's own record
already has that) and not a precomputed diff (derivable on demand from any two
stored snapshots, or refetched live via rich_revision.fetch_diff_html/action=compare).
"""

import os
import re
import json
import time

from wikihow_scraper import ARTICLES_DIR
from wikihow_scraper.history import fetch_full_history
from wikihow_scraper.articles import get_top_level_category
from wikihow_scraper.rich_revision import fetch_wikitext, _REVERT_PATTERNS
from wikihow_scraper.tabs import claim_new_tab, attach_driver, detach_driver_safely


def _parse_iso_timestamp(display_ts):
    """Best-effort parse of "22:23, 1 May 2026" -> "2026-05-01T22:23:00". Returns
    None (not the display string) if the format doesn't match, so callers can tell
    the difference between "parsed" and "unavailable"."""
    try:
        t = time.strptime(display_ts, "%H:%M, %d %B %Y")
        return time.strftime("%Y-%m-%dT%H:%M:%S", t)
    except (ValueError, TypeError):
        return None


def _slugify_category(category):
    return re.sub(r"[^A-Za-z0-9]+", "-", category).strip("-")


def _detect_content_type(wikitext):
    """
    WikiHow pages that aren't step-by-step "how-to" articles - confirmed so far:
    quizzes, marked by a __QUIZ__ magic word in their own wikitext and holding a
    <quizdata>{JSON} block instead of prose steps. They live in the SAME main
    article namespace as real articles (not filterable by namespace/colon checks
    the way User:/Video: pages are), and come back with an empty/missing category
    breadcrumb - get_top_level_category() reports them as "Uncategorized".
    Returns None (not "article") when wikitext isn't available to check, so callers
    can tell "confirmed article" apart from "couldn't tell" rather than guessing.
    """
    if not wikitext:
        return None
    return "quiz" if "__QUIZ__" in wikitext else "article"


def scrape_article_to_json(article_title, port=9099, max_revisions=None, include_snapshots=True,
                            progress_callback=None, should_stop=None):
    """
    Full pipeline for one article:
      1. Determine its top-level category (for the folder path)
      2. Fetch its full revision history (metadata only - cheap)
      3. For each revision (optionally capped at max_revisions, newest first),
         fetch its wikitext snapshot (expensive - one page load per revision)
      4. Write everything to data/articles/<Category>/<Article>.json

    max_revisions: cap on how many of the newest revisions get a wikitext snapshot
    fetched (snapshots are the expensive part - one extra page load each). None =
    all of them, which is fine for short histories but slow for 1000+ revision
    articles. Metadata for ALL revisions is still collected either way.

    include_snapshots: set False to skip wikitext fetching entirely (fast - metadata
    only, no snapshot_wikitext field on any revision).

    progress_callback: optional callable(current, total) invoked after each snapshot
    fetch - lets a caller (e.g. the TUI dashboard) show live "N/total" progress
    instead of only seeing a result once the whole article finishes.

    should_stop: optional callable() -> bool, checked before each snapshot fetch (the
    slow part - a single article can have thousands of revisions). If it returns
    True, the loop stops early and the JSON is still written with whatever revisions
    were completed so far, flagged with "stopped_early": true - so a Stop button
    actually stops mid-article instead of only preventing the NEXT article's batch
    from starting (confirmed gap: without this, "Stop" during a 10,000-revision
    article would do nothing until that single article finished on its own).

    Returns the path to the written JSON file.
    """
    print(f"[*] Determining category for '{article_title}'...")
    category = get_top_level_category(article_title, port=port)
    print(f"[*] Category: {category}")

    print(f"[*] Fetching full revision history...")
    history = fetch_full_history(article_title, port=port, sort="recent_to_old")
    print(f"[*] {len(history)} revisions found.")

    driver = None
    if include_snapshots:
        driver = attach_driver(port)
        # Claim a dedicated tab under the shared lock before this thread starts using
        # `driver` for repeated fetch_wikitext() calls - without this, a concurrently
        # running scrape_article_to_json() for a DIFFERENT article would silently
        # fight over whichever tab happens to be "current" (this call never opened
        # its own), reproducing the exact cross-contamination bug found in history.py.
        my_tab = claim_new_tab(driver)
        driver.switch_to.window(my_tab)

    revisions_out = []
    snapshot_limit = max_revisions if max_revisions is not None else len(history)

    stopped_early = False
    stop_snapshots_from = None  # once set, every revision from here on is recorded
    # with metadata only (cheap - already fetched via fetch_full_history) but its
    # snapshot fetch (the slow part) is skipped, rather than dropping the revision
    # from the file entirely. This is what makes a stop RESUMABLE: update_article_json()
    # already knows how to backfill any existing revision missing snapshot_wikitext -
    # if we instead just cut revisions_out short at the stop point (the earlier
    # behavior), those un-fetched revisions would never even be recorded, and
    # resuming later would have no way to find them (the "new revisions" check only
    # looks NEWER than what's known, and these are all OLDER than what we already have,
    # since history is newest-first) - the article would look "complete" with a gap.
    try:
        for idx, rev in enumerate(history):
            if stop_snapshots_from is None and should_stop and should_stop():
                stopped_early = True
                stop_snapshots_from = idx

            parent = history[idx + 1] if idx + 1 < len(history) else None
            comment = rev.get("comment") or ""

            restored_match = re.search(r"revision #(\d+)", comment)

            record = {
                "revision_id": rev.get("revid"),
                "parent_id": parent.get("revid") if parent else None,
                "timestamp": _parse_iso_timestamp(rev.get("timestamp_display")),
                "timestamp_raw": rev.get("timestamp_display"),
                "edit_reason": comment,
                "is_minor": rev.get("is_minor_edit"),
                "size_bytes": rev.get("size_bytes"),
                "size_delta": rev.get("delta_bytes"),
                "author": {
                    "name": rev.get("user"),
                    "user_page": rev.get("user_profile_url"),
                },
                "parent_author": {
                    "name": parent.get("user") if parent else None,
                    "user_page": parent.get("user_profile_url") if parent else None,
                },
                "diff_url": (
                    f"https://www.wikihow.com/index.php?title={article_title}"
                    f"&diff={rev.get('revid')}&oldid={parent.get('revid')}"
                ) if parent else None,
                "is_revert": bool(_REVERT_PATTERNS.search(comment)) if comment else False,
                "restored_revision_id": restored_match.group(1) if restored_match else None,
            }

            should_fetch_snapshot = (
                include_snapshots and idx < snapshot_limit
                and (stop_snapshots_from is None or idx < stop_snapshots_from)
            )
            if should_fetch_snapshot:
                record["snapshot_wikitext"] = fetch_wikitext(article_title, rev.get("revid"), driver=driver)
                if progress_callback:
                    progress_callback(idx + 1, snapshot_limit)
                if (idx + 1) % 10 == 0:
                    print(f"  [snapshot {idx + 1}/{snapshot_limit}] revid={rev.get('revid')}")

            revisions_out.append(record)
    finally:
        if driver:
            if len(driver.window_handles) > 1:
                try:
                    driver.close()
                except Exception:
                    pass
            detach_driver_safely(driver)

    category_slug = _slugify_category(category)
    out_dir = os.path.join(ARTICLES_DIR, category_slug)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{article_title}.json")

    actual_snapshots = sum(1 for r in revisions_out if r.get("snapshot_wikitext") is not None) if include_snapshots else 0
    # Detected from the newest revision's snapshot (revisions_out[0], since history is
    # newest-first) - that's normally the first one fetched, so this works in the
    # common case. Comes back None (not "article") when no snapshot was fetched at all
    # (e.g. include_snapshots=False or max_revisions=0) - see _detect_content_type().
    content_type = _detect_content_type(revisions_out[0].get("snapshot_wikitext")) if revisions_out else None
    payload = {
        "article": article_title,
        "category": category,
        "content_type": content_type,
        "revision_count": len(revisions_out),
        "snapshots_included": actual_snapshots,
        "stopped_early": stopped_early,
        "revisions": revisions_out,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    if stopped_early:
        print(
            f"[!] Stopped early - saved: {out_path} - full metadata for all {len(revisions_out)} revisions, "
            f"but only {actual_snapshots} have a wikitext snapshot. Run 'update' on this article later to "
            f"resume fetching the rest - it backfills exactly the missing snapshots, no re-fetching."
        )
    else:
        print(f"[+] Saved: {out_path}")
    return out_path


def _find_existing_json(article_title):
    """Searches every category folder under ARTICLES_DIR for <article_title>.json -
    the category is only known once fetched, so this avoids re-fetching it just to
    locate a file we already saved under some category last time."""
    for category_slug in os.listdir(ARTICLES_DIR):
        candidate = os.path.join(ARTICLES_DIR, category_slug, f"{article_title}.json")
        if os.path.exists(candidate):
            return candidate
    return None


def update_article_json(article_title, port=9099, fetch_missing_snapshots=True, max_new_snapshots=None,
                         should_stop=None):
    """
    Incrementally updates an already-scraped article instead of re-scraping
    everything from zero:
      1. Loads the existing JSON (if none exists, falls back to a full
         scrape_article_to_json() instead).
      2. Fetches fresh history metadata and finds revisions NEWER than the highest
         revision_id already stored - only those get fetched/added.
      3. If fetch_missing_snapshots, also backfills snapshot_wikitext for any
         EXISTING revision that doesn't have one yet (e.g. it was captured by a
         metadata-only or max_revisions-capped run previously).
      4. Re-writes the same JSON file with the merged result.

    This is the "update technique" - re-visiting an article later only costs
    fetching what's actually new/missing, not the entire history again.

    max_new_snapshots: cap on how many NEW revisions get a snapshot fetched this
    run (missing-snapshot backfills are not capped by this - only genuinely new
    revisions are).

    Returns the path to the updated JSON file.
    """
    existing_path = _find_existing_json(article_title)
    if not existing_path:
        print(f"[*] No existing data for '{article_title}' - doing a full scrape instead.")
        return scrape_article_to_json(article_title, port=port, max_revisions=max_new_snapshots)

    with open(existing_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    existing_revisions = payload["revisions"]
    existing_by_id = {r["revision_id"]: r for r in existing_revisions}
    highest_known_id = max(existing_by_id.keys(), default=0)

    print(f"[*] Existing data: {len(existing_revisions)} revisions, highest known revid={highest_known_id}")
    print(f"[*] Fetching current full history to find anything new...")
    history = fetch_full_history(article_title, port=port, sort="recent_to_old")

    new_history_entries = [r for r in history if r.get("revid", 0) > highest_known_id]
    print(f"[*] {len(new_history_entries)} new revision(s) found since last scrape.")

    missing_snapshot_ids = {
        r["revision_id"] for r in existing_revisions
        if fetch_missing_snapshots and not r.get("snapshot_wikitext")
    }
    print(f"[*] {len(missing_snapshot_ids)} existing revision(s) missing a snapshot to backfill.")

    if not new_history_entries and not missing_snapshot_ids:
        print("[*] Nothing to update - already fully up to date.")
        return existing_path

    driver = None
    if new_history_entries or missing_snapshot_ids:
        driver = attach_driver(port)
        my_tab = claim_new_tab(driver)
        driver.switch_to.window(my_tab)

    new_records = []
    try:
        snapshot_cap = max_new_snapshots if max_new_snapshots is not None else len(new_history_entries)
        for idx, rev in enumerate(new_history_entries):
            # A new entry's parent is either the next new entry (raw history.py schema:
            # revid/user/user_profile_url), or - for the oldest new entry - the newest
            # of the already-known revisions (OUR schema: revision_id/author{name,
            # user_page}). Normalize both shapes to the same (id, name, page) tuple so
            # parent_id/parent_author are correct either way, instead of silently
            # reading the wrong keys off whichever shape happened to be there.
            if idx + 1 < len(new_history_entries):
                p = new_history_entries[idx + 1]
                parent_id, parent_name, parent_page = p.get("revid"), p.get("user"), p.get("user_profile_url")
            elif existing_revisions:
                p = existing_revisions[0]
                parent_id = p.get("revision_id")
                parent_name = p.get("author", {}).get("name")
                parent_page = p.get("author", {}).get("user_page")
            else:
                parent_id = parent_name = parent_page = None

            comment = rev.get("comment") or ""
            restored_match = re.search(r"revision #(\d+)", comment)

            record = {
                "revision_id": rev.get("revid"),
                "parent_id": parent_id,
                "timestamp": _parse_iso_timestamp(rev.get("timestamp_display")),
                "timestamp_raw": rev.get("timestamp_display"),
                "edit_reason": comment,
                "is_minor": rev.get("is_minor_edit"),
                "size_bytes": rev.get("size_bytes"),
                "size_delta": rev.get("delta_bytes"),
                "author": {"name": rev.get("user"), "user_page": rev.get("user_profile_url")},
                "parent_author": {"name": parent_name, "user_page": parent_page},
                "diff_url": (
                    f"https://www.wikihow.com/index.php?title={article_title}"
                    f"&diff={rev.get('revid')}&oldid={parent_id}"
                ) if parent_id else None,
                "is_revert": bool(_REVERT_PATTERNS.search(comment)) if comment else False,
                "restored_revision_id": restored_match.group(1) if restored_match else None,
            }

            if idx < snapshot_cap and not (should_stop and should_stop()):
                record["snapshot_wikitext"] = fetch_wikitext(article_title, rev.get("revid"), driver=driver)

            new_records.append(record)

        for revid in missing_snapshot_ids:
            if should_stop and should_stop():
                print(f"[!] Stop requested - backfill halted early, remaining missing snapshots stay missing "
                      f"for next time (nothing lost, nothing re-fetched unnecessarily).")
                break
            existing_by_id[revid]["snapshot_wikitext"] = fetch_wikitext(article_title, revid, driver=driver)
    finally:
        if driver:
            if len(driver.window_handles) > 1:
                try:
                    driver.close()
                except Exception:
                    pass
            detach_driver_safely(driver)

    merged_revisions = new_records + existing_revisions
    payload["revisions"] = merged_revisions
    payload["revision_count"] = len(merged_revisions)
    payload["snapshots_included"] = sum(1 for r in merged_revisions if r.get("snapshot_wikitext"))
    if not payload.get("content_type") and merged_revisions:
        # Backfills content_type on older JSON files scraped before this field existed,
        # or ones where it came back None the first time (no snapshot was available
        # then). merged_revisions[0] is the newest revision either way (new_records are
        # all newer than existing_revisions, which was already newest-first).
        payload["content_type"] = _detect_content_type(merged_revisions[0].get("snapshot_wikitext"))

    with open(existing_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"[+] Updated: {existing_path} (+{len(new_records)} new, backfilled {len(missing_snapshot_ids)})")
    return existing_path
