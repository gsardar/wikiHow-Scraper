"""
Shared rate-limit / block detection - one canonical place, used by every fetch path
(the old HTTP-based scraper.py, and the newer Selenium-based history.py/
rich_revision.py/articles.py), instead of each one having its own copy of the
heuristic. A block often comes back as HTTP 200 with a Cloudflare/"Access Denied"
page rather than a clean 4xx/5xx, so text-signature checks matter as much as status
codes - and Selenium doesn't expose the HTTP status code at all, so text-signature
detection is REQUIRED (not just a nice-to-have) for the CDP-attached fetch paths.
"""

import re

_BLOCK_TEXT_SIGNATURES = re.compile(
    r"attention required.{0,20}cloudflare"
    r"|<title>\s*access denied\s*</title>"
    r"|checking your browser before accessing"
    r"|you have been blocked"
    r"|unusual traffic from your"
    r"|please verify you are a human"
    r"|rate limit exceeded",
    re.IGNORECASE,
)

BLOCK_STATUS_CODES = {403, 429, 503}


class BlockedError(Exception):
    """Raised when a fetch's response looks like a rate-limit/block page rather
    than real content - callers should NOT treat the returned text as valid data."""

    def __init__(self, message, status_code=None, signature_snippet=None):
        super().__init__(message)
        self.status_code = status_code
        self.signature_snippet = signature_snippet


def is_blocked_text(html_text):
    """True if this page's TEXT looks like a block/challenge page, regardless of
    what HTTP status code came with it (or whether we even have one, as with
    Selenium page_source)."""
    if not html_text:
        return False
    return bool(_BLOCK_TEXT_SIGNATURES.search(html_text))


def check_response(response):
    """For requests.Response objects: raises BlockedError if the status code or
    body text indicates a block. Returns silently (no return value) if clean."""
    if response.status_code in BLOCK_STATUS_CODES or is_blocked_text(response.text):
        snippet = response.text[:500]
        raise BlockedError(
            f"Blocked: HTTP {response.status_code}",
            status_code=response.status_code,
            signature_snippet=snippet,
        )


def check_page_source(html_text, context=""):
    """For Selenium page_source (no status code available): raises BlockedError if
    the text matches a known block signature. `context` (e.g. a URL or revid) is
    folded into the message to make the raised error traceable to its source."""
    if is_blocked_text(html_text):
        snippet = html_text[:500]
        raise BlockedError(
            f"Blocked (text signature matched){': ' + context if context else ''}",
            status_code=None,
            signature_snippet=snippet,
        )
