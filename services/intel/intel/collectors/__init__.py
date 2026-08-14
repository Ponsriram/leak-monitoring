"""Collectors: fetch pages from a source."""

from __future__ import annotations

from .base import Collector, FetchedPage, page_url
from .html import to_text
from .tor_http import TorHttpCollector

__all__ = [
    "Collector",
    "FetchedPage",
    "TorHttpCollector",
    "get_collector",
    "page_url",
    "to_text",
]


def get_collector(kind: str, **kwargs: object) -> Collector:
    """Build a collector by kind. `browser` is imported lazily — it needs the extra."""
    if kind == "http":
        return TorHttpCollector(**kwargs)  # type: ignore[arg-type]

    if kind == "browser":
        from .tor_browser import TorBrowserCollector  # noqa: PLC0415 - optional extra

        socks_ports = kwargs.pop("socks_ports", None)
        if socks_ports:
            kwargs["socks_port"] = socks_ports[0]  # type: ignore[index]
        kwargs.pop("max_retries", None)
        return TorBrowserCollector(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"Unknown collector {kind!r}. Available: http, browser")
