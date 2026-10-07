"""Seeding a source's listing pages into the frontier, all at once.

Pagination in this project is declared, not discovered: `sources.yaml` gives each source a
`pagination_style` and `max_pages`, and `page_url` turns those into URLs. So a cycle does not
walk to page 2 after finding page 1 — it queues every page it expects up front and lets the
workers take them concurrently.

How many pages "expects" means:

* the first time, or whenever no page has ever returned real content: `max_pages`;
* afterwards: `min(max_pages, deepest page ever seen + 2)`. The +2 is what notices a listing
  that grew, without queueing `max_pages` jobs every cycle for a listing that stayed short.

Whether a queued page is actually fetched this cycle is decided by the queue, not here: a page
that is not yet due (page 1 every probe interval, deeper pages every deep interval) is left
alone, and one that is past the end of the listing is pruned when the boundary is found.
"""

from __future__ import annotations

import structlog

from ..collectors.base import page_url
from ..storage import SourceRow
from .frontier import host_of, is_allowed, normalize_crawl_url
from .queue import CrawlQueue, EnqueueResult, NewUrl

log = structlog.get_logger(__name__)

# Past the deepest page seen, how many more are tried on each cycle.
LOOKAHEAD_PAGES = 2


def pages_to_seed(max_pages: int, observed_depth: int | None, pagination_style: str) -> int:
    """How many listing pages to queue for a source this cycle.

    >>> pages_to_seed(10, None, "query")
    10
    >>> pages_to_seed(10, 4, "query")
    6
    >>> pages_to_seed(10, 9, "query")
    10
    >>> pages_to_seed(5, None, "none")
    1
    """
    if max_pages < 1:
        return 0
    if pagination_style == "none":
        return 1  # the base URL is the whole listing
    if observed_depth is None or observed_depth < 1:
        return max_pages
    return min(max_pages, observed_depth + LOOKAHEAD_PAGES)


def listing_urls(source: SourceRow, count: int) -> list[NewUrl]:
    """Pages 1..count of a source's listing, validated like any other URL."""
    base = source.crawl_url
    hosts = {h for h in (host_of(source.base_url), host_of(source.active_url or "")) if h}

    urls: list[NewUrl] = []
    for page_no in range(1, count + 1):
        url = page_url(base, page_no, source.pagination_style)
        if url is None:
            break
        if not is_allowed(url, allowed_hosts=hosts):
            # A configured address that is not http(s), or that is internal: not crawled,
            # and said so rather than silently dropped.
            log.warning("seed url refused", source=source.slug, url=url)
            break
        urls.append(
            NewUrl(
                url=url,
                url_normalized=normalize_crawl_url(url),
                kind="listing",
                page_no=page_no,
                depth=0,
            )
        )
    return urls


async def seed_source(
    queue: CrawlQueue, cycle_id: int, source: SourceRow, *, force: bool = False
) -> EnqueueResult:
    """Queue this source's listing pages for the cycle.

    Pages that are not yet due are left alone, so on an ordinary cycle only what has
    come due is queued. `force` — a manual Sync — queues them all regardless: "sync
    now" means now."""
    observed = await queue.observed_depth(source.id)
    count = pages_to_seed(source.max_pages, observed, source.pagination_style)
    result = await queue.enqueue(cycle_id, source.id, listing_urls(source, count), force=force)
    log.info(
        "source seeded",
        source=source.slug,
        pages=count,
        observed_depth=observed,
        new=result.inserted,
        due_again=result.requeued,
    )
    return result
