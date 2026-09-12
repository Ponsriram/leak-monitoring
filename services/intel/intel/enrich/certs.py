"""Subdomains, from certificate transparency logs.

Every publicly-trusted TLS certificate issued since 2018 is published to append-only logs.
That makes CT the one place where a company's internal-facing hostnames — `vpn.`, `mail.`,
`remote.`, `citrix.` — become a matter of public record, because somebody had to get a
certificate for them.

For a victim that just appeared on a leak site, that list is the fastest read on what their
external surface looks like, and it comes from asking a log, not from touching the victim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog

from .domains import normalize_domain

log = structlog.get_logger(__name__)

__all__ = ["CertFacts", "fetch_certificates"]

_CRTSH_URL = "https://crt.sh/"

# A large estate can have thousands of certified hostnames and crt.sh will return every one.
# Past a point the list stops informing anyone and starts being a payload to store, render
# and scroll, so it is cut here rather than in the UI.
_MAX_SUBDOMAINS = 200


@dataclass(slots=True)
class CertFacts:
    subdomains: list[str] = field(default_factory=list)
    # True when the log had more than we kept, so the UI can say so instead of implying
    # the list is complete.
    truncated: bool = False

    def is_empty(self) -> bool:
        return not self.subdomains


async def fetch_certificates(client: httpx.AsyncClient, domain: str) -> CertFacts:
    """Ask crt.sh for every hostname certified under a domain.

    crt.sh is a single volunteer-run service and it is regularly slow or briefly down. That
    is handled the same way as a registry outage: return nothing, let the orchestrator record
    the enricher as unavailable, and let the rest of the hunt stand. Certificate data is
    valuable but never the reason a search succeeds or fails.
    """
    response = await client.get(
        _CRTSH_URL,
        params={"q": f"%.{domain}", "output": "json"},
        headers={"Accept": "application/json"},
        # Overrides the client-wide 10s read budget, which crt.sh missed on every single
        # hunt during the first live run — it answers a wildcard query by scanning a very
        # large index, and ten seconds is simply the wrong expectation for it. Nothing else
        # in a hunt waits on this: findings stream as each enricher returns, so the leak
        # matches, WHOIS and liveness are on screen long before this one lands.
        timeout=httpx.Timeout(connect=5.0, read=25.0, write=5.0, pool=5.0),
    )

    if response.status_code >= 400:
        log.debug("crt.sh: lookup failed", domain=domain, status=response.status_code)
        return CertFacts()

    try:
        payload = response.json()
    except ValueError:
        # crt.sh serves an HTML error page under load, with a 200.
        log.debug("crt.sh: non-json response", domain=domain)
        return CertFacts()

    if not isinstance(payload, list):
        return CertFacts()

    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        # One certificate covers many names, newline-separated in this field.
        raw_names = entry.get("name_value")
        if not isinstance(raw_names, str):
            continue
        for raw in raw_names.splitlines():
            # Wildcards are a real certificate but not a real host — `*.example.com` cannot
            # be probed or looked up, and normalizing it yields a bogus hostname.
            if raw.startswith("*."):
                raw = raw[2:]
            name = normalize_domain(raw)
            if name and (name == domain or name.endswith(f".{domain}")):
                seen.add(name)

    ordered = sorted(seen)
    return CertFacts(
        subdomains=ordered[:_MAX_SUBDOMAINS],
        truncated=len(ordered) > _MAX_SUBDOMAINS,
    )
