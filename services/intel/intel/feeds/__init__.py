"""Public indicator feeds.

Everything here is a clearnet HTTPS fetch of a published, free feed — no keys, no scraping,
no Tor. That is a different collection posture from the leak-site crawler and the reason these
live in their own package: the crawler reaches criminal infrastructure over Tor under an
advisory lock, and a feed fetch is an ordinary GET of a file somebody publishes for exactly
this purpose.

    urlhaus     malicious URLs (abuse.ch)
    threatfox   indicators tied to malware families (abuse.ch)

Each collector returns normalized `FeedIoc` records. Deciding what an indicator *is* happens
here, at the edge, so `storage` only ever sees rows that already match the database's enum —
a feed inventing a new `ioc_type` next month should fail in the collector that knows about it,
not somewhere in the middle of an upsert.
"""

from .base import FeedIoc, host_of
from .threatfox import fetch_threatfox
from .urlhaus import fetch_urlhaus

__all__ = ["FeedIoc", "fetch_threatfox", "fetch_urlhaus", "host_of"]
