"""What counts as "the same page", and which URLs may become jobs at all.

Two jobs for one logical page means two Tor fetches of the same site, so normalization is
what keeps the queue honest. It is deliberately conservative: a leak site's `?page=2` and
`?id=7` are different pages, so query parameters are kept, and only the ones that are known
to be tracking noise are dropped.

URLs only ever enter the queue from two places — a source's own configured address and the
pages derived from it, and links found on pages already fetched from that source — and
`is_allowed` is the check both go through.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..collectors.links import is_skipped_path, looks_like_file

# Parameters that identify a visit rather than a page. Exact names plus the utm_ family.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref_src"})
_TRACKING_PREFIXES = ("utm_",)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_crawl_url(url: str) -> str:
    """The identity of a page.

    - scheme and host lowercased, default port dropped
    - fragment dropped
    - trailing slash dropped (the root stays `/`)
    - tracking parameters dropped; every other parameter kept and sorted

    >>> normalize_crawl_url("HTTP://Example.onion:80/page/?utm_source=x&b=2&a=1#top")
    'http://example.onion/page?a=1&b=2'
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"

    path = parts.path.rstrip("/") or "/"

    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(pairs))

    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# Hostnames that mean "this machine or this network" whatever they resolve to.
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def is_internal_host(host: str) -> bool:
    """Is this host an address the crawler must never connect to?

    Loopback, private, link-local (which includes cloud metadata at 169.254.169.254),
    unspecified, multicast and reserved IP literals, plus the names that stand for them.
    A source's allowlist cannot override this: a configured source pointing at an internal
    address is a mistake or an attack, and either way is not crawled.
    """
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP literal: a name such as an .onion address
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def is_allowed(url: str, *, allowed_hosts: set[str]) -> bool:
    """May this URL become a crawl job?

    Only plain http(s) to a host that belongs to the source, and never to an internal
    address. Nothing a crawled page says can widen that set, which is what keeps the crawler
    from becoming a way to reach arbitrary addresses (the internal network, cloud metadata,
    anything else). Seeded URLs and discovered links both go through here.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    try:
        host = (parts.hostname or "").lower()
    except ValueError:  # a malformed netloc
        return False
    if not host or is_internal_host(host):
        return False
    return host in {h.lower() for h in allowed_hosts}


def admit_link(url: str, *, allowed_hosts: set[str]) -> bool:
    """May this *discovered* link become a crawl job? `is_allowed`, plus: it is a page.

    A second check on top of `extract_links`' own filtering, because this is the gate that
    actually decides what enters the queue and it should not depend on a caller having
    filtered first. Files are the leaked data itself and are never fetched; account and
    navigation pages are never listings.
    """
    if not is_allowed(url, allowed_hosts=allowed_hosts):
        return False
    parts = urlsplit(url)
    return not looks_like_file(parts.path) and not is_skipped_path(parts.path, parts.query)


def listing_boundary(
    *,
    kind: str,
    page_no: int | None,
    result: str,
    http_status: int | None,
    ok: bool,
    final_failure: bool,
) -> int | None:
    """If this outcome says a source's listing ends here, the last page that exists.

    Pages after the returned number are not worth fetching. None means "no conclusion", which
    is the safe answer: pruning is only for evidence of an end, never for doubt.

    * Page 1 failing for good — gated, empty, refused, not found, or still failing after every
      retry — means the source is not serving a listing: nothing beyond page 1 is worth a Tor
      request. (Page 1 merely *retrying* is not a conclusion; the claim gate holds the rest.)
    * A later page that came back empty is the existing definition of the end of a listing:
      cleaned text under `MIN_PAGE_TEXT_CHARS`.
    * A later page that is 404 or 410 does not exist.
    * Anything else on a later page — a timeout, a 5xx, a 403, a gate, an oversized body — is
      NOT evidence the listing ended. The old crawler treated every failed fetch as the end,
      and a timeout on page 4 would have skipped a perfectly good page 5.
    """
    if kind != "listing":
        return None
    page = page_no or 1

    if page == 1:
        return 1 if (final_failure or result == "empty") else None

    if ok and result == "empty":
        return page - 1
    if final_failure and http_status in (404, 410):
        return page - 1
    return None
