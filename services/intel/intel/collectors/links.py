"""Pick which links on a page are worth following.

A leak site is a tree: the listing links to a page per victim, and that page links to file
indexes, proofs and more pages. Following links is what turns "the listing" into "the site",
and it is also how a crawler wanders off into things it should never touch — so this module
is mostly a list of what to refuse:

* **Another host.** A link is followed only if it stays on the host being crawled. The text
  of a crawled page decides nothing about where the crawler connects (the same rule the
  mirror logic applies to announced addresses).
* **A file.** Archives, dumps, images, torrents and the like are the leaked data itself.
  Fetching one is a download of stolen material, and a multi-gigabyte one at that. The
  crawler reads pages, never payloads.
* **Account and navigation pages.** Login, register, contact — nothing there is a listing.

Pure and synchronous so it can be tested without a network.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.parser import HTMLParser

# Extensions that mean "this is a file", not "this is a page".
_FILE_EXTENSIONS = frozenset(
    {
        "7z",
        "apk",
        "avi",
        "bak",
        "bin",
        "bz2",
        "csv",
        "dmp",
        "doc",
        "docx",
        "exe",
        "gif",
        "gz",
        "ico",
        "img",
        "iso",
        "jpeg",
        "jpg",
        "json",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "pdf",
        "png",
        "ppt",
        "pptx",
        "rar",
        "sql",
        "svg",
        "tar",
        "tgz",
        "torrent",
        "txt",
        "webm",
        "webp",
        "xls",
        "xlsx",
        "xml",
        "xz",
        "zip",
    }
)

# Path words for pages that are never a listing or a victim page.
_SKIP_PATH = re.compile(
    r"/(?:log-?in|log-?out|sign-?in|sign-?up|register|contact|rules|faq|donate|download|"
    r"api|static|assets|cdn-cgi)(?:/|$|\?)",
    re.IGNORECASE,
)

_MAX_URL_CHARS = 512


def normalize_url(url: str) -> str:
    """The form used to decide whether two links are the same page.

    Fragment dropped, host lowercased, a trailing slash ignored: `/a`, `/a/` and `/a#top`
    are one page, and fetching it three times is what a visited-set exists to prevent.
    """
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}{query}"


def extract_links(
    html: str,
    page_url: str,
    *,
    visited: set[str] | None = None,
    limit: int = 5,
) -> list[str]:
    """Up to `limit` followable links from `html`, in the order the page presents them.

    `visited` holds `normalize_url` forms already fetched or queued; those are skipped. The
    set is not modified — the caller decides when a link counts as taken.
    """
    if limit < 1:
        return []

    host = urlsplit(page_url).netloc.lower()
    seen = visited or set()
    picked: list[str] = []
    picked_keys: set[str] = set()

    for node in HTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "magnet:")):
            continue

        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)

        if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
            continue
        if len(absolute) > _MAX_URL_CHARS:
            continue
        if _is_file(parts.path) or _SKIP_PATH.search(parts.path + ("?" if parts.query else "")):
            continue

        key = normalize_url(absolute)
        if key in seen or key in picked_keys:
            continue

        picked.append(urldefrag(absolute)[0])
        picked_keys.add(key)
        if len(picked) >= limit:
            break

    return picked


def looks_like_file(path: str) -> bool:
    """Is this path a file download rather than a page?"""
    return _is_file(path)


def is_skipped_path(path: str, query: str = "") -> bool:
    """Is this an account or navigation page that is never a listing or a victim page?"""
    return _SKIP_PATH.search(path + ("?" if query else "")) is not None


def _is_file(path: str) -> bool:
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    return last.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS
