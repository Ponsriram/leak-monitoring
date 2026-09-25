"""Public feeds.

Everything here is a clearnet HTTPS fetch of something published for free — no keys, no
scraping, no Tor. That is a different collection posture from the leak-site crawler and the
reason these live in their own package: the crawler reaches criminal infrastructure over Tor
under an advisory lock, and a feed fetch is an ordinary GET of a file somebody publishes for
exactly this purpose.

    urlhaus          malicious URLs (abuse.ch)                          -> iocs
    threatfox        indicators tied to malware families (abuse.ch)     -> iocs
    tweetfeed        indicators researchers post on X (tweetfeed.live)  -> iocs
    ransomware_live  victims posted on ransomware leak sites            -> leaks
    scam_reports     scam phone numbers in public Mastodon / Reddit     -> mobile_numbers
                     posts

Each collector returns normalized records. Deciding what a value *is* happens here, at the
edge, so `storage` only ever sees rows that already match the database's shape — a feed
inventing a new type next month should fail in the collector that knows about it, not
somewhere in the middle of an upsert.
"""

from .base import FeedIoc, host_of
from .ransomware_live import fetch_ransomware_live
from .scam_reports import ScamReport, fetch_mastodon_reports, fetch_reddit_reports
from .threatfox import fetch_threatfox
from .tweetfeed import fetch_tweetfeed
from .urlhaus import fetch_urlhaus

__all__ = [
    "FeedIoc",
    "ScamReport",
    "fetch_mastodon_reports",
    "fetch_ransomware_live",
    "fetch_reddit_reports",
    "fetch_threatfox",
    "fetch_tweetfeed",
    "fetch_urlhaus",
    "host_of",
]
