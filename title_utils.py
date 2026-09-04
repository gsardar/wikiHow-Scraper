"""
Title slug sanitization & URL parsing utilities for WikiHow articles.
Strips query strings (?oldid=..., ?action=...), anchors (#step_1), and domain prefixes,
returning clean, canonical article title slugs.
"""

import re
from urllib.parse import urlparse, parse_qs, unquote


def clean_title_slug(val):
    """
    Sanitizes any raw string, URL, or query path into a clean WikiHow article slug.
    Examples:
      - "https://www.wikihow.com/index.php?title=Cute-Things-to-Do-for-Your-Girlfriend&oldid=18489804&action=raw"
        -> "Cute-Things-to-Do-for-Your-Girlfriend"
      - "https://www.wikihow.com/Make-Hybrid-Plants?action=history"
        -> "Make-Hybrid-Plants"
      - "/Cook-Beetroot#Step_1"
        -> "Cook-Beetroot"
    """
    if not val:
        return ""

    val = str(val).strip()

    # Handle full or relative URLs
    if "://" in val or val.startswith("/") or "index.php" in val:
        parsed = urlparse(val)
        qs = parse_qs(parsed.query)
        if "title" in qs and qs["title"]:
            val = qs["title"][0]
        elif parsed.path:
            val = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    # Strip query parameters if any remain
    if "?" in val:
        val = val.split("?", 1)[0]

    # Strip URL anchor fragments
    if "#" in val:
        val = val.split("#", 1)[0]

    # Strip index.php prefix if raw
    if val.startswith("index.php"):
        val = ""

    # Unescape URL encoding
    val = unquote(val).strip()

    # Standardize spaces to dashes
    val = val.replace(" ", "-")

    # Remove leading/trailing dashes/slashes
    val = val.strip("-/")

    return val
