"""Collector interface and page-walking logic shared by every collector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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

    The old crawler appended `?page=N` to every site unconditionally, including ones with no
    pagination at all — which is why it produced runs of identical pages and had to detect
    them after the fact by comparing content. Pagination is now declared per source in
    `sources.yaml`; `none` means the base URL is the whole listing.
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
