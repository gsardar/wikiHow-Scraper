"""
Full article revision-history extraction, paginating through WikiHow's action=history
pages until every revision is collected. Requires an authenticated browser session for
articles that gate history behind login (confirmed: most do).

Natural sort order is newest-first ("recent to old") - that's how MediaWiki's history
page already lists revisions, so no reordering is needed for that direction; pass
sort="old_to_recent" to reverse the final list if the oldest-first order is wanted instead.
"""

import re
import time
from bs4 import BeautifulSoup

from wikihow_scraper.tabs import claim_new_tab, attach_driver, detach_driver_safely
from wikihow_scraper.block_detection import check_page_source


def _parse_revision(li, article_title):
    revid = li.get("data-mw-revid")
    date_link = li.select_one(".mw-changeslist-date")
    user_link = li.select_one(".mw-userlink")
    size_span = li.select_one(".history-size")
    delta_span = li.select_one(".mw-plusminus-pos, .mw-plusminus-neg, .mw-plusminus-null")
    comment_span = li.select_one(".comment")
    minor = bool(li.select_one(".minoredit"))
    undo = bool(li.select_one(".mw-history-undo a"))
    rollback = bool(li.select_one(".mw-rollback-link"))

    size_text = size_span.get_text(strip=True) if size_span else None
    size_bytes = None
    if size_text:
        match = re.search(r"([\d,]+)", size_text)
        if match:
            size_bytes = int(match.group(1).replace(",", ""))

    delta_text = delta_span.get_text(strip=True) if delta_span else None
    delta_bytes = None
    if delta_text:
        try:
            delta_bytes = int(delta_text.replace(",", "").replace("+", ""))
        except ValueError:
            delta_bytes = None

    return {
        "article": article_title,
        "revid": int(revid) if revid else None,
        "timestamp_display": date_link.get_text(strip=True) if date_link else None,
        "user": user_link.get_text(strip=True) if user_link else None,
        "user_profile_url": user_link.get("href") if user_link else None,
        "size_bytes": size_bytes,
        "delta_bytes": delta_bytes,
        "is_minor_edit": minor,
        "comment": comment_span.get_text(strip=True) if comment_span else None,
        "has_undo_link": undo,
        "has_rollback_link": rollback,
    }


def fetch_full_history(article_title, port=9099, sort="recent_to_old", page_size=500,
                        max_pages=None, polite_delay=1.0):
    """
    Paginates through an article's FULL edit history via an already-running,
    CDP-attachable browser (e.g. the wikihow_scraper watchdog instance).

    sort: "recent_to_old" (default, MediaWiki's native order) or "old_to_recent"
          (reverses the final collected list).
    page_size: revisions per page request (WikiHow's UI offers 20/50/100/250/500).
    max_pages: safety cap on how many pages to fetch (None = no cap, follow "older" links
               until exhausted).
    polite_delay: seconds to sleep between page requests.

    Returns a list of revision dicts (see _parse_revision for the schema).
    """
    driver = attach_driver(port)

    all_revisions = []
    seen_revids = set()
    offset = ""
    page_count = 0

    try:
        # Serialize only the tab-creation moment - window.open() + window_handles[-1]
        # races under concurrency (a thread can grab a tab a DIFFERENT thread just
        # opened). This caused two different articles fetched concurrently to silently
        # return identical data from whichever tab won the race - confirmed via
        # Cook-Rice/Change-a-Tire both reporting the same creation revision even though
        # they are genuinely distinct articles.
        my_tab = claim_new_tab(driver)
        driver.switch_to.window(my_tab)

        while True:
            url = (
                f"https://www.wikihow.com/index.php?title={article_title}"
                f"&action=history&limit={page_size}"
            )
            if offset:
                url += f"&offset={offset}"

            driver.get(url)
            time.sleep(2)

            check_page_source(driver.page_source, context=url)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            history_list = soup.select("#pagehistory li")

            new_count = 0
            for li in history_list:
                revid = li.get("data-mw-revid")
                if revid and revid not in seen_revids:
                    seen_revids.add(revid)
                    all_revisions.append(_parse_revision(li, article_title))
                    new_count += 1

            page_count += 1
            print(f"  [page {page_count}] +{new_count} new revisions (total so far: {len(all_revisions)})")

            if new_count == 0:
                break  # nothing new - we've reached the end or hit a loop

            # Find the genuine "older N" navigation link by its TEXT, not just an
            # offset=/limit= href pattern - the per-page-size links (20/50/100/250/500)
            # ALSO match that pattern (with an empty or stale offset), and picking one
            # of those by mistake barely advances the offset each time instead of
            # jumping a full page forward (discovered: this silently degraded pagination
            # to +1 revision per request after the first 2 real pages).
            older_link = next(
                (a for a in soup.select("a[href*='offset=']")
                 if "older" in a.get_text(strip=True).lower()),
                None
            )
            if not older_link:
                break

            match = re.search(r"offset=(\d+)", older_link.get("href", ""))
            if not match:
                break
            new_offset = match.group(1)
            if new_offset == offset:
                break  # not making progress - stop rather than loop forever
            offset = new_offset

            if max_pages and page_count >= max_pages:
                print(f"  [stopped] reached max_pages={max_pages}")
                break

            time.sleep(polite_delay)

    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)

    if sort == "old_to_recent":
        all_revisions.reverse()

    return all_revisions


def summarize_by_editor(revisions, sort="most_edits"):
    """
    Groups revisions by user and ranks them by edit count on this article.
    sort: "most_edits" (default, descending) or "least_edits" (ascending).

    Returns a list of dicts: {user, edit_count, total_bytes_added, total_bytes_removed,
    first_edit_display, last_edit_display}, ordered per `sort`.
    """
    by_user = {}
    for rev in revisions:
        user = rev.get("user") or "(unknown)"
        entry = by_user.setdefault(user, {
            "user": user,
            "edit_count": 0,
            "total_bytes_added": 0,
            "total_bytes_removed": 0,
            "first_edit_display": None,
            "last_edit_display": None,
        })
        entry["edit_count"] += 1
        delta = rev.get("delta_bytes") or 0
        if delta > 0:
            entry["total_bytes_added"] += delta
        elif delta < 0:
            entry["total_bytes_removed"] += -delta
        # revisions arrive recent-to-old from fetch_full_history's native order,
        # so the LAST one seen per user is their earliest edit, the FIRST seen is latest.
        if entry["last_edit_display"] is None:
            entry["last_edit_display"] = rev.get("timestamp_display")
        entry["first_edit_display"] = rev.get("timestamp_display")

    summary = list(by_user.values())
    summary.sort(key=lambda e: e["edit_count"], reverse=(sort == "most_edits"))
    return summary
