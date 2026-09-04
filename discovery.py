"""
Article discovery: ways to generate a list of article titles to feed the continuous
scraper WITHOUT the user having to type exact titles by hand - confirmed working
against real WikiHow pages:
  - random_articles(n)      : Special:Randomizer, hit N times
  - recent_changes(n)       : Special:RecentChanges - site-wide, most-recently-edited
                               order comes for free (no per-article history fetch needed)
  - category_articles(cat)  : a category page's own article listing
  - new_pages(n)            : Special:NewPages - site-wide, newest-CREATED-first,
                               natively sorted by WikiHow's own backend (confirmed;
                               Special:PopularPages/by-views is NOT available - disabled)
  - most_revisions(n)       : Special:MostRevisions - site-wide, most-edited-first,
                               natively ranked by WikiHow's own backend
  - ancient_pages(n)        : Special:AncientPages - site-wide, oldest-since-last-edit
"""

import re
import time
from bs4 import BeautifulSoup

from wikihow_scraper.tabs import claim_new_tab, attach_driver, detach_driver_safely
from wikihow_scraper.block_detection import check_page_source


def _attach_new_tab(port):
    driver = attach_driver(port)
    my_tab = claim_new_tab(driver)
    driver.switch_to.window(my_tab)
    return driver


def random_articles(n, port=9099):
    """Returns n distinct article titles via WikiHow's own randomizer."""
    driver = _attach_new_tab(port)
    titles = []
    try:
        attempts = 0
        while len(titles) < n and attempts < n * 3:
            attempts += 1
            driver.get("https://www.wikihow.com/Special:Randomizer")
            time.sleep(1.5)
            check_page_source(driver.page_source, context="Special:Randomizer")
            title = driver.current_url.rstrip("/").rsplit("/", 1)[-1]
            if title and title not in titles and not title.startswith("Special:"):
                titles.append(title)
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)
    return titles


def random_in_category(category, n, port=9099):
    """
    Returns n distinct article titles via Special:RandomInCategory/<category> -
    WikiHow's own category-scoped randomizer. Confirmed: fails silently (stays on
    the picker form, no redirect) for an invalid/non-existent category slug - always
    pass an exact real category name (e.g. from articles.get_top_level_category()
    or the known top-level category list), not free text.
    """
    driver = _attach_new_tab(port)
    titles = []
    try:
        attempts = 0
        while len(titles) < n and attempts < n * 3:
            attempts += 1
            driver.get(f"https://www.wikihow.com/Special:RandomInCategory/{category}")
            time.sleep(1.5)
            check_page_source(driver.page_source, context=f"Special:RandomInCategory/{category}")
            if "Special:RandomInCategory" in driver.current_url:
                raise ValueError(f"'{category}' isn't a valid WikiHow category (or has no articles).")
            title = driver.current_url.rstrip("/").rsplit("/", 1)[-1]
            if title and title not in titles:
                titles.append(title)
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)
    return titles


def recent_changes(n=30, port=9099):
    """
    Returns up to n article titles from Special:RecentChanges, in the order WikiHow
    lists them (most recently edited first) - this IS the "most recently edited"
    sequencing option, site-wide, without needing to fetch any article's full history
    just to learn its latest edit time.
    """
    driver = _attach_new_tab(port)
    try:
        driver.get(f"https://www.wikihow.com/index.php?title=Special:RecentChanges&limit={n * 3}")
        time.sleep(2)
        check_page_source(driver.page_source, context="Special:RecentChanges")
        soup = BeautifulSoup(driver.page_source, "html.parser")

        titles = []
        seen = set()
        for a in soup.select("a.mw-changeslist-title"):
            href = a.get("href", "")
            # Some rows' "title" link is actually a diff/user/video link sharing the
            # same CSS class - only keep clean "/Article-Title" hrefs (no query
            # string, no namespace prefix like Video:/User:/index.php).
            if not href.startswith("/") or "?" in href:
                continue
            title = href.lstrip("/")
            if ":" in title or not title or title in seen:
                continue
            seen.add(title)
            titles.append(title)
            if len(titles) >= n:
                break
        return titles
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def _ordered_list_items(driver, url, n, context):
    """Shared scraper for MediaWiki's <ol class="special"><li><a>...</a> ... report
    pages (MostRevisions, AncientPages) - WikiHow's own backend already returns them
    in the page's ranked order, so no client-side sorting is needed."""
    driver.get(url)
    time.sleep(1.5)
    check_page_source(driver.page_source, context=context)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    titles = []
    for li in soup.select("ol.special li"):
        a = li.select_one("a[href^='/']")
        if not a:
            continue
        href = a.get("href", "")
        if "?" in href or ":" in href.lstrip("/"):
            continue
        title = href.lstrip("/")
        if title:
            titles.append(title)
        if len(titles) >= n:
            break
    return titles


def new_pages(n=20, port=9099):
    """
    Returns up to n article titles from Special:NewPages, in the order WikiHow lists
    them (most recently CREATED first) - namespace=0 restricts to real articles
    (excludes User:/Talk:/Category: etc. pages that also get "created").
    """
    driver = _attach_new_tab(port)
    try:
        driver.get(f"https://www.wikihow.com/index.php?title=Special:NewPages&limit={n * 2}&namespace=0")
        time.sleep(1.5)
        check_page_source(driver.page_source, context="Special:NewPages")
        soup = BeautifulSoup(driver.page_source, "html.parser")

        titles = []
        seen = set()
        for li in soup.select("li[data-mw-revid]"):
            a = li.select_one("a[href^='/index.php?title=']")
            if not a:
                continue
            match = re.search(r"title=([^&]+)", a.get("href", ""))
            if not match:
                continue
            title = match.group(1)
            if title not in seen:
                seen.add(title)
                titles.append(title)
            if len(titles) >= n:
                break
        return titles
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def most_revisions(n=20, port=9099):
    """Returns up to n article titles from Special:MostRevisions, in WikiHow's own
    most-edited-first order (site-wide, no per-article history fetch needed)."""
    driver = _attach_new_tab(port)
    try:
        return _ordered_list_items(
            driver, f"https://www.wikihow.com/index.php?title=Special:MostRevisions&limit={n}",
            n, "Special:MostRevisions",
        )
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def fewest_revisions(n=20, port=9099):
    """Returns up to n article titles from Special:FewestRevisions, in WikiHow's own
    fewest-edited-first order (site-wide) - complements most_revisions()."""
    driver = _attach_new_tab(port)
    try:
        return _ordered_list_items(
            driver, f"https://www.wikihow.com/index.php?title=Special:FewestRevisions&limit={n}",
            n, "Special:FewestRevisions",
        )
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def ancient_pages(n=20, port=9099):
    """Returns up to n article titles from Special:AncientPages, in WikiHow's own
    oldest-since-last-edit-first order (site-wide)."""
    driver = _attach_new_tab(port)
    try:
        return _ordered_list_items(
            driver, f"https://www.wikihow.com/index.php?title=Special:AncientPages&limit={n}",
            n, "Special:AncientPages",
        )
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)


def category_articles(category, limit=None, port=9099):
    """
    Returns article titles listed on a category page (e.g. category="Ties" or
    "Personal-Care-and-Style"). Only real article links inside the main content
    area - navigation chrome and Category:/Special:/Talk:/User: links are excluded.
    """
    driver = _attach_new_tab(port)
    try:
        driver.get(f"https://www.wikihow.com/Category:{category}")
        time.sleep(2)
        check_page_source(driver.page_source, context=f"Category:{category}")
        soup = BeautifulSoup(driver.page_source, "html.parser")

        main = soup.select_one("#mw-content-text, #bodyContent, .mw-parser-output")
        if not main:
            return []

        titles = []
        seen = set()
        for a in main.select("a[href^='/']"):
            href = a.get("href", "")
            if any(x in href for x in ("Category:", "Special:", "Talk:", "User:", "index.php")):
                continue
            title = href.lstrip("/")
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
            if limit and len(titles) >= limit:
                break
        return titles
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)
