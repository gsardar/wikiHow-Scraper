"""
Article-level operations: sorting a set of articles by creation date, newest or
oldest first. Complements history.py (which sorts REVISIONS within one article) by
sorting the ARTICLES themselves.

NOTE: originally this used api.php's prop=revisions (rvdir=newer&rvlimit=1) to fetch
just the FIRST revision per article in one cheap request. Confirmed by direct testing
that WikiHow's api.php returns HTTP 500 for ANY content-touching query module
(prop=revisions, prop=info, list=categorymembers all fail) while pure metadata queries
(meta=siteinfo) succeed - this looks like those modules are broken or blocked
server-side, not a client-side parameter bug. So creation-date lookup falls back to
the browser-rendered action=history page (reusing history.fetch_full_history and
reading its oldest entry) instead.
"""

import threading
import time
from bs4 import BeautifulSoup

from wikihow_scraper import get_adaptive_worker_count
from wikihow_scraper.history import fetch_full_history
from wikihow_scraper.tabs import claim_new_tab, attach_driver, detach_driver_safely
from wikihow_scraper.block_detection import check_page_source


def get_top_level_category(article_title, port=9099):
    """
    Fetches an article's TOP-LEVEL WikiHow category from its own page's breadcrumb
    (e.g. "Personal Care and Style" for Tie-a-Tie, whose full breadcrumb is
    Personal Care and Style > Fashion > Fashion Accessories > Ties). Used to place
    the article's data under data/articles/<top-level-category>/.

    Returns the category name (str), or "Uncategorized" if none is found.
    """
    driver = attach_driver(port)

    try:
        my_tab = claim_new_tab(driver)
        driver.switch_to.window(my_tab)

        driver.get(f"https://www.wikihow.com/{article_title}")
        time.sleep(2)
        check_page_source(driver.page_source, context=article_title)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        cat_links = soup.select("a[href^='/Category:']")
        if not cat_links:
            return "Uncategorized"
        return cat_links[0].get_text(strip=True)
    except Exception:
        return "Uncategorized"
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def get_creation_info(article_title, port=9099):
    """
    Fetches an article's creation info (its oldest/first revision) by pulling its full
    history via the browser (api.php's page-content queries are broken - see module
    docstring) and taking the last entry in recent-to-old order.

    Returns a dict: {article, first_revid, first_timestamp, first_user} or
    {article, error} if the article doesn't exist / the fetch fails.
    """
    try:
        revisions = fetch_full_history(article_title, port=port, sort="recent_to_old")
        if not revisions:
            return {"article": article_title, "error": "No revisions found"}
        oldest = revisions[-1]
        return {
            "article": article_title,
            "first_revid": oldest.get("revid"),
            "first_timestamp": oldest.get("timestamp_display"),
            "first_user": oldest.get("user"),
        }
    except Exception as e:
        return {"article": article_title, "error": str(e)}


def sort_articles_by_creation(article_titles, order="recent_to_old", port=9099, max_workers=None):
    """
    Fetches creation info for each article CONCURRENTLY (tab-workers in the same
    browser instance) and returns them sorted by creation date.

    NOTE: since this now pulls each article's FULL history to find its creation date
    (api.php shortcut unavailable), this is relatively expensive for articles with long
    histories - keep max_workers modest and expect this to take real time for more than
    a handful of articles.

    max_workers: if not given, picked adaptively from currently available RAM (tab-mode
    estimate, since these are extra tabs in one existing browser) via
    get_adaptive_worker_count() - not a fixed number.

    order: "recent_to_old" (default) or "old_to_recent".
    """
    if max_workers is None:
        max_workers = get_adaptive_worker_count(mode="tab")

    results = {}
    lock = threading.Lock()

    def worker(title):
        info = get_creation_info(title, port=port)
        with lock:
            results[title] = info

    # Simple batching to respect max_workers without a full thread-pool dependency.
    for i in range(0, len(article_titles), max_workers):
        batch = article_titles[i:i + max_workers]
        threads = [threading.Thread(target=worker, args=(t,)) for t in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    ordered = [results[t] for t in article_titles]
    valid = [r for r in ordered if "error" not in r]
    invalid = [r for r in ordered if "error" in r]

    # first_timestamp here is a display string ("22:23, 1 May 2026"), not sortable
    # lexically by date - use the revid instead, which is monotonically increasing
    # with time on MediaWiki and gives the correct chronological order.
    valid.sort(key=lambda r: r["first_revid"] or 0, reverse=(order == "recent_to_old"))
    return valid, invalid
