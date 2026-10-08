"""Collector interface and page-walking logic shared by every collector."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import httpx

FetchKind = Literal["ok", "transient", "permanent"]


@dataclass(slots=True)
class FetchedPage:
    url: str
    page_no: int
    text: str
    byte_size: int


@runtime_checkable
class Collector(Protocol):
    """Fetches a source's pages and returns cleaned text."""

    name: str

    # Why the most recent `fetch` returned None. Read by the pipeline so a failed crawl
    # records the actual reason instead of a generic "could not fetch".
    last_error: str | None

    async def fetch(self, url: str) -> str | None:
        """Return raw HTML, or None if the page could not be fetched."""
        ...

    async def aclose(self) -> None: ...


def page_url(base_url: str, page_no: int, style: str) -> str | None:
    """Build the URL for page N.

    Pagination is declared per source in `sources.yaml`; `none` means the base URL is the
    whole listing. Appending `?page=N` to a site with no pagination only fetches runs of
    identical pages.
    """
    if page_no == 1:
        return base_url

    match style:
        case "none":
            return None
        case "query":  # ?page=2
            separator = "&" if "?" in base_url else "?"
            return f"{base_url}{separator}page={page_no}"
        case "path":  # /page/2
            return f"{base_url.rstrip('/')}/page/{page_no}"
        case "offset":  # ?offset=25 (assumes 25/page)
            separator = "&" if "?" in base_url else "?"
            return f"{base_url}{separator}offset={(page_no - 1) * 25}"
        case _:
            return None


@dataclass(slots=True)
class CapturedJson:
    """A JSON response the page loaded by itself (XHR/fetch) while it rendered."""

    url: str
    body: str


@dataclass(slots=True)
class FetchResult:
    """One fetch attempt, described fully enough for the queue to decide what happens next.

    `kind` is the whole decision: `ok` — a page worth processing; `transient` — failed in a way
    another attempt may fix; `permanent` — will not improve by retrying. The collector only
    *classifies*. Whether and when to try again is the queue's decision, never the collector's.
    """

    url: str
    kind: FetchKind
    text: str | None = None
    http_status: int | None = None
    response_ms: int = 0
    size_bytes: int = 0
    content_type: str | None = None
    error: str | None = None
    # Same-host JSON the page fetched while rendering (browser collector only). None when it
    # fetched none, or the collector cannot see the page's own requests.
    json_responses: list[CapturedJson] | None = None


# Statuses where trying again can plausibly succeed. 408/425/429 are the server asking for a
# later try; every 5xx is a server that failed to answer rather than one refusing us.
_TRANSIENT_STATUS = frozenset({408, 425, 429})

# Content types that are pages. A missing Content-Type is treated as a page: plenty of onion
# services send none, and refusing them would drop sources that work today.
_PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


def classify_status(status: int) -> FetchKind:
    """HTTP status -> ok / transient / permanent.

    >>> [classify_status(s) for s in (200, 301, 404, 410, 403, 429, 500, 503)]
    ['ok', 'ok', 'permanent', 'permanent', 'permanent', 'transient', 'transient', 'transient']
    """
    if status < 400:
        return "ok"
    if status in _TRANSIENT_STATUS or status >= 500:
        return "transient"
    # 404, 410, 403 and every other 4xx: the server answered and said no on purpose. Retrying
    # a 403 three times is how a source used to spend six minutes being refused three times.
    return "permanent"


def is_page_content_type(content_type: str | None) -> bool:
    """Is this a response worth parsing as a web page?"""
    if not content_type:
        return True
    return content_type.split(";", 1)[0].strip().lower() in _PAGE_TYPES


def classify_exception(exc: BaseException) -> FetchKind:
    """A transport failure -> transient / permanent. Not every exception is worth a retry.

    Transient: timeouts, and the network and proxy failures Tor produces constantly — a
    rendezvous circuit that would not build ("TTL expired"), a service that is momentarily
    unreachable, a connection reset mid-read.
    Permanent: a URL that is malformed or not http(s), a redirect loop, a body that cannot be
    decoded, and anything unrecognised — an unknown error repeated three times is still an
    unknown error, and retrying it only hides the bug.
    """
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.TooManyRedirects)):
        return "permanent"
    if isinstance(exc, (httpx.LocalProtocolError, httpx.DecodingError)):
        return "permanent"
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError)):
        return "transient"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "transient"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "permanent"
