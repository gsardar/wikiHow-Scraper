"""
Rich per-revision extraction: full wikitext snapshots (before/after) plus a structured
diff, matching the richer schema style the user wants (revision_id, parent_id,
snapshot_wikitext, parent_snapshot_wikitext, changes[], is_revert, etc.) rather than
just the history-list metadata history.py captures.

Two api.php/index.php endpoints make this possible even though api.php's
prop=revisions is broken (see articles.py docstring):
  - index.php?action=raw&oldid=X   -> full wikitext of revision X
  - api.php?action=compare&fromrev=A&torev=B -> diff_html between two revisions
"""

import re
import time
from bs4 import BeautifulSoup

from wikihow_scraper.tabs import claim_new_tab, attach_driver, detach_driver_safely
from wikihow_scraper.block_detection import check_page_source

_REVERT_PATTERNS = re.compile(
    r"revert(ed)?|undo|undid|rollback|rv\b|restore(d)? (a |the )?(previous|prior|earlier) (version|revision)",
    re.IGNORECASE,
)


def _attach_new_tab(port):
    driver = attach_driver(port)
    my_tab = claim_new_tab(driver)
    driver.switch_to.window(my_tab)
    return driver


def fetch_wikitext(article_title, revid, port=9099, driver=None):
    """Fetches the full wikitext of one specific revision via action=raw."""
    own_driver = driver is None
    if own_driver:
        driver = _attach_new_tab(port)
    try:
        url = f"https://www.wikihow.com/index.php?title={article_title}&oldid={revid}&action=raw"
        driver.get(url)
        time.sleep(1.5)
        body_text = driver.find_element("tag name", "body").text
        check_page_source(body_text, context=url)
        return body_text
    finally:
        if own_driver:
            if len(driver.window_handles) > 1:
                try:
                    driver.close()
                except Exception:
                    pass
            detach_driver_safely(driver)


def fetch_diff_html(from_revid, to_revid, port=9099, driver=None):
    """Fetches the raw diff HTML between two revisions via api.php's action=compare."""
    own_driver = driver is None
    if own_driver:
        driver = _attach_new_tab(port)
    try:
        url = f"https://www.wikihow.com/api.php?action=compare&fromrev={from_revid}&torev={to_revid}&format=json"
        driver.get(url)
        time.sleep(1.5)
        import json
        body = driver.find_element("tag name", "body").text
        check_page_source(body, context=url)
        data = json.loads(body)
        return data.get("compare", {}).get("*", "")
    finally:
        if own_driver:
            if len(driver.window_handles) > 1:
                try:
                    driver.close()
                except Exception:
                    pass
            detach_driver_safely(driver)


def parse_diff_html(diff_html):
    """
    Parses MediaWiki's diff table HTML into a list of structured {op, before, after}
    change blocks - op is "replace" when both sides have content, "insert" when only
    the "after" side does, "delete" when only "before" does.
    """
    if not diff_html:
        return []

    soup = BeautifulSoup(diff_html, "html.parser")
    changes = []

    rows = soup.find_all("tr")
    i = 0
    while i < len(rows):
        row = rows[i]
        deleted = row.select_one("td.diff-deletedline")
        added = row.select_one("td.diff-addedline")

        if deleted or added:
            before_text = deleted.get_text(" ", strip=True) if deleted else None
            after_text = added.get_text(" ", strip=True) if added else None

            # A "replace" often spans TWO rows in MediaWiki's table layout (one row can
            # carry only the deleted side, the next only the added side) - merge them
            # if the next row supplies the missing side.
            if before_text and not after_text and i + 1 < len(rows):
                next_added = rows[i + 1].select_one("td.diff-addedline")
                if next_added and not rows[i + 1].select_one("td.diff-deletedline"):
                    after_text = next_added.get_text(" ", strip=True)
                    i += 1

            if not before_text and not after_text:
                # Neither side has real content - a stray/empty diff row (seen in
                # practice on some revisions), not a genuine change. Skip it rather
                # than record a meaningless {"op": "insert", "before": "", "after": null}.
                i += 1
                continue

            op = "replace" if (before_text and after_text) else ("delete" if before_text else "insert")
            changes.append({"op": op, "before": before_text or None, "after": after_text or None})

        i += 1

    return changes


def fetch_rich_revision(article_title, revid, parent_revid, comment=None,
                         user=None, parent_user=None, timestamp=None,
                         size_bytes=None, delta_bytes=None, is_minor=None, port=9099):
    """
    Builds one rich revision record combining:
      - metadata passed in (typically already known from history.py's cheaper extraction)
      - full wikitext snapshot of this revision AND its parent
      - a structured diff (changes[]) between them
      - an is_revert heuristic based on the edit comment

    Reuses ONE attached driver/tab for all three network calls this revision needs
    (wikitext x2 + diff x1) instead of opening a fresh tab per call.
    """
    driver = _attach_new_tab(port)
    try:
        snapshot = fetch_wikitext(article_title, revid, driver=driver)
        parent_snapshot = fetch_wikitext(article_title, parent_revid, driver=driver) if parent_revid else None
        diff_html = fetch_diff_html(parent_revid, revid, driver=driver) if parent_revid else None
        changes = parse_diff_html(diff_html) if diff_html else []

        is_revert = bool(comment and _REVERT_PATTERNS.search(comment))
        restored_match = re.search(r"revision #(\d+)", comment) if comment else None
        restored_revision_id = restored_match.group(1) if restored_match else None

        return {
            "article": article_title,
            "revision_id": revid,
            "parent_id": parent_revid,
            "timestamp_display": timestamp,
            "user": user,
            "parent_user": parent_user,
            "comment": comment,
            "is_minor": is_minor,
            "size_bytes": size_bytes,
            "delta_bytes": delta_bytes,
            "snapshot_wikitext": snapshot,
            "parent_snapshot_wikitext": parent_snapshot,
            "changes": changes,
            "change_count": len(changes),
            "is_revert": is_revert,
            "restored_revision_id": restored_revision_id,
        }
    finally:
        if len(driver.window_handles) > 1:
            try:
                driver.close()
            except Exception:
                pass
        detach_driver_safely(driver)
