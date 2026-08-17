"""Find onion addresses mentioned on a page.

Leak sites rotate addresses constantly — seized, blocked, or simply abandoned — and they
announce the replacement on the old site while it is still reachable. That announcement is
the only advance warning there is, and until now it was thrown away with the rest of the
page text.

This module only *finds* addresses. Deciding whether to follow one is a separate decision
made in the pipeline, because a "our new address is X" banner is text written by the site
being crawled: treating it as an instruction would let a crawled host choose where the
crawler connects next.
"""

from __future__ import annotations

import re

# v3 onion addresses only: exactly 56 characters of base32, then ".onion". v2 addresses
# (16 characters) have been unroutable since 2021, and a 16-character pattern also matches
# a lot of ordinary base32-looking noise.
_ONION_RE = re.compile(
    r"\b(?:(https?)://)?([a-z2-7]{56}\.onion)\b(/[^\s\"'<>)\]]*)?",
    re.IGNORECASE,
)


def onion_host(url: str) -> str | None:
    """The bare onion host inside a URL, lowercased. None if there isn't one."""
    match = _ONION_RE.search(url)
    return match.group(2).lower() if match else None


# Wording that turns an address on a page into an announcement about where this site lives.
#
# The distinction is load-bearing: leak sites link to plenty of onion addresses that are
# nothing to do with them — negotiation portals, other crews, escrow, forums. Only an
# address presented as *this site's* other address is ever a failover candidate, so an
# address with none of this wording near it stays a plain observation.
_ANNOUNCEMENT_RE = re.compile(
    r"\b(mirror|mirrors|new\s+(?:address|domain|site|url)|backup|alternative|"
    r"we\s+(?:have\s+)?moved|moved\s+to|our\s+(?:new\s+)?(?:site|address|domain)|"
    r"official\s+(?:site|address|domain))\b",
    re.IGNORECASE,
)

# Context is scoped to lines, not to a character window. A fixed window bled across
# boundaries: "…update your bookmarks.\n\nChat with support: <other address>" put
# announcement wording within reach of an address it had nothing to do with, which is
# exactly the misclassification that must not happen here.
#
# Look at the address's own line and up to this many preceding non-empty lines, skipping
# over lines that are themselves just addresses — that is what a footer list of mirrors
# under a single "Our mirrors:" heading looks like.
_ANNOUNCEMENT_LOOKBACK = 3


def find_onion_urls(text: str, *, exclude_hosts: set[str] | None = None) -> dict[str, str]:
    """Map onion host -> full URL for every v3 address in `text`.

    Deduplicated by host, keeping the first spelling seen, so a page that repeats its mirror
    in a banner and again in a footer produces one candidate rather than two. Hosts in
    `exclude_hosts` are dropped — that is how a page's own address, and the addresses of
    sources already monitored, stay out of the candidate list.
    """
    announced, other = classify_onion_urls(text, exclude_hosts=exclude_hosts)
    return {**announced, **other}


def classify_onion_urls(
    text: str, *, exclude_hosts: set[str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Split the addresses on a page into (announced as ours, merely present).

    Only the first group is ever a failover candidate. An address in the second group is
    recorded too — it is still intelligence, and an operator can promote it by hand — but
    nothing acts on it automatically.
    """
    excluded = {host.lower() for host in (exclude_hosts or set())}
    announced: dict[str, str] = {}
    other: dict[str, str] = {}

    lines = text.splitlines()

    for index, line in enumerate(lines):
        for match in _ONION_RE.finditer(line):
            scheme, host, path = match.group(1), match.group(2).lower(), match.group(3)
            if host in excluded or host in announced or host in other:
                continue

            # Default to http: an onion address is authenticated by the address itself,
            # and most of these services do not serve TLS at all.
            url = f"{(scheme or 'http').lower()}://{host}{path or ''}"

            if _is_announcement(lines, index):
                announced[host] = url
            else:
                other[host] = url

    return announced, other


def _is_announcement(lines: list[str], index: int) -> bool:
    """Does the address on `lines[index]` come with wording claiming it as this site's?"""
    if _ANNOUNCEMENT_RE.search(lines[index]):
        return True

    checked = 0
    for previous in reversed(lines[:index]):
        stripped = previous.strip()
        if not stripped:
            continue

        has_address = _ONION_RE.search(stripped) is not None
        # A line that is *only* an address is another entry in the same list — keep walking
        # back to whatever heading introduced them.
        if has_address and _ONION_RE.fullmatch(stripped):
            continue
        # A line carrying an address *and* prose has already spent its wording on its own
        # address. "Our new address is X" says nothing about the next address down the page,
        # and treating it as though it did is how an unrelated link gets promoted.
        if has_address:
            return False

        if _ANNOUNCEMENT_RE.search(stripped):
            return True
        checked += 1
        if checked >= _ANNOUNCEMENT_LOOKBACK:
            break

    return False
