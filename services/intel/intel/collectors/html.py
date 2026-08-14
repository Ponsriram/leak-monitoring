"""HTML → clean text, with selectolax.

selectolax parses roughly 10–30× faster than BeautifulSoup, which the old crawler used. That
matters here because the pipeline re-parses on every crawl.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

# Elements that never contain listing data but do contain a lot of text.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "head")

# Elements whose content changes on every request (live countdowns, view counters). They are
# removed before hashing so a ticking clock doesn't make every page look "changed" and
# trigger a pointless re-extraction each cycle.
_VOLATILE_SELECTORS = (
    ".countdown",
    "[class*='countdown']",
    "[class*='timer']",
    "[id*='countdown']",
    "[id*='timer']",
)


def to_text(html: str, *, drop_volatile: bool = True) -> str:
    """Extract readable text from a page."""
    tree = HTMLParser(html)

    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    if drop_volatile:
        for selector in _VOLATILE_SELECTORS:
            try:
                for node in tree.css(selector):
                    node.decompose()
            except Exception:  # noqa: BLE001 - selectolax raises on odd selectors
                continue

    body = tree.body or tree.root
    if body is None:
        return ""

    text = body.text(separator="\n", strip=True)
    return _clean(text)


def _clean(text: str) -> str:
    """Drop non-printable and non-ASCII noise, collapse blank runs.

    The old code did `ord(char) < 128`, which also deleted every accented character in
    European company names — turning "Nestlé" into "Nestl". Keep printable Unicode; drop
    only control characters and the decorative symbols these sites are full of.
    """
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = "".join(
            char
            for char in raw_line
            if char.isprintable() and not (0x2500 <= ord(char) <= 0x2BFF)
        ).strip()
        if line:
            lines.append(line)

    # Collapse repeated blank lines that survived stripping.
    return "\n".join(lines)
