"""How a source's pages get divided into concurrent fetch waves.

The crawler used to walk a listing strictly in order: fetch page 1, wait, fetch page 2,
wait, … up to `max_pages`. Over Tor a single page fetch is 10-30 seconds, so a ten-page
source cost ten sequential round trips — O(P) in wall clock, and no amount of cross-source
concurrency helped a source that happened to be deep.

The obstacle to just firing all P pages at once is that P is not known. A listing ends when
a page comes back empty, and that answer only exists after the fetch. Requesting all
`max_pages` unconditionally trades the sequential cost for a different one: `max_pages` is
typically much larger than the real page count, so most of those fetches are wasted Tor
circuits against a site we are trying not to hammer.

So: **galloping search**. Fetch in waves whose size doubles — 1, then `width`, then
`2·width`, `4·width` … — and stop at the first wave containing the end of the listing.
Reaching page P takes O(log(P/width)) sequential rounds instead of O(P), and because the
last wave is the only one that can overshoot, no more than ~2P pages are ever requested.
That is the same bound `bisect.insort`-style galloping gives on an unbounded array, applied
to pagination.

Kept in its own module because it is pure arithmetic and worth testing on its own; the
pipeline's job is I/O.
"""

from __future__ import annotations

from collections.abc import Iterator

# Never grow a wave past this, however many pages a source declares. A wave is a burst of
# simultaneous requests at one onion service; doubling without a ceiling would eventually
# put a 64-request burst on a site that answers 3 pages, which is both rude and a good way
# to be rate-limited off a source entirely.
DEFAULT_WAVE_CAP = 16


def page_waves(
    max_pages: int,
    *,
    width: int,
    cap: int = DEFAULT_WAVE_CAP,
) -> Iterator[list[int]]:
    """Yield page numbers grouped into waves that may be fetched concurrently.

    Page 1 is always alone in the first wave. It is the page that decides whether the source
    is reachable at all, whether we need to fail over to a mirror, and whether what came
    back is a listing or a JS challenge — every one of which changes the address the rest of
    the pages would be fetched from. Speculating on pages 2-5 of an address we are about to
    abandon is work thrown away.

    >>> list(page_waves(10, width=4))
    [[1], [2, 3, 4, 5], [6, 7, 8, 9, 10]]
    >>> list(page_waves(1, width=4))
    [[1]]
    """
    if max_pages < 1:
        return

    yield [1]

    size = max(1, width)
    limit = max(1, cap)
    page = 2

    while page <= max_pages:
        end = min(page + size - 1, max_pages)
        yield list(range(page, end + 1))
        page = end + 1
        size = min(size * 2, limit)
