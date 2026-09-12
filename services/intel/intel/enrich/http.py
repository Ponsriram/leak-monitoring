"""The shared clearnet HTTP client for enrichment.

Deliberately not the Tor collector. Every lookup in this package targets public
infrastructure — a registry's RDAP endpoint, a certificate transparency log, the victim's own
home page — none of which is hidden, and all of which answer in well under a second directly
while a Tor circuit costs twenty to thirty. Routing them through Tor would make an
interactive search feel broken to save nothing: the sites being asked about are public, and
the questions are ones any browser asks.

One client per hunt, reused across enrichers, so connection setup is paid once.
"""

from __future__ import annotations

import httpx

__all__ = ["ENRICH_TIMEOUT", "USER_AGENT", "enrich_client"]

"""Identify the crawler honestly.

A generic or spoofed browser agent on automated requests is what gets a research project
blocked and, worse, makes its traffic indistinguishable from something that ought to be
blocked. Saying what this is costs nothing and lets an operator who notices the requests
work out who to contact.
"""
USER_AGENT = "leak-monitoring/0.1 (threat-intel research; passive enrichment)"

# Short on purpose. A hunt runs several of these concurrently while a person waits, so a
# slow registry has to fail fast rather than hold the whole result page hostage.
ENRICH_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


def enrich_client() -> httpx.AsyncClient:
    """A client for passive lookups.

    `follow_redirects` is on because RDAP bootstrap is *built* on redirects — rdap.org
    answers every domain with a 302 to the registry that actually holds the record — and
    because a victim domain that 301s to www or to https is still very much live.
    """
    return httpx.AsyncClient(
        timeout=ENRICH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        # A redirect chain longer than this is a loop or a captive portal, not a site.
        max_redirects=5,
    )
