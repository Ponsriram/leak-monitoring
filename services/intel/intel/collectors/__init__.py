"""Collectors: fetch pages from a source."""

from __future__ import annotations

from .base import Collector, FetchedPage, page_url
from .html import to_text
from .onion import classify_onion_urls, find_onion_urls, onion_host
from .tor_http import TorHttpCollector

__all__ = [
    "Collector",
    "FetchedPage",
    "TorHttpCollector",
    "classify_onion_urls",
    "find_onion_urls",
    "get_collector",
    "onion_host",
    "page_url",
    "to_text",
]

# Arguments the browser collector has no equivalent for. Dropped rather than passed, so the
# pipeline can hand every collector the same settings without knowing which kind it built.
_HTTP_ONLY_KWARGS = ("max_retries", "backoff_seconds", "backoff_cap_seconds")


def get_collector(kind: str, **kwargs: object) -> Collector:
    """Build a collector by kind. `browser` is imported lazily — it needs the extra."""
    if kind == "http":
        return TorHttpCollector(**kwargs)  # type: ignore[arg-type]

    if kind == "browser":
        from .tor_browser import TorBrowserCollector  # noqa: PLC0415 - optional extra

        socks_ports = kwargs.pop("socks_ports", None)
        if socks_ports:
            kwargs["socks_port"] = socks_ports[0]  # type: ignore[index]
        for key in _HTTP_ONLY_KWARGS:
            kwargs.pop(key, None)
        return TorBrowserCollector(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"Unknown collector {kind!r}. Available: http, browser")
