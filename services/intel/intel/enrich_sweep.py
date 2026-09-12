"""Background enrichment of every victim domain we hold.

A hunt enriches one domain because somebody asked about it. This sweep enriches the rest, so
the Ransomware and Dark Web tables can show WHOIS, technologies and site status for every row
instead of only the handful anyone has searched for. Without it those columns are structurally
empty and the pages look broken rather than unpopulated.

Three deliberate differences from a hunt:

**No certificate transparency.** crt.sh takes twenty seconds and is the slowest thing in a
hunt by an order of magnitude. Paying that for hundreds of domains nobody has asked about
would consume the sweep's whole budget on the least-requested data. It stays interactive-only.

**Small batches, on a timer.** The sweep exists to fill a backlog over hours, not to finish
fast. Enriching 751 domains as quickly as possible means a burst of outbound requests that
looks exactly like a scan to everyone receiving it.

**Oldest first, never-checked before stale.** A domain nobody has ever looked at is worth more
than refreshing one checked yesterday.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from .config import Settings
from .enrich import apex_domain, enrich_client, fetch_rdap, probe_domain, resolve_domain
from .storage import Storage

log = structlog.get_logger(__name__)

__all__ = ["SweepResult", "sweep_domains"]


@dataclass(slots=True)
class SweepResult:
    attempted: int = 0
    enriched: int = 0
    failed: int = 0


async def _enrich_one(client: Any, domain: str) -> dict[str, Any]:
    """Gather the facts for one domain. Never raises — a failure is a recorded state.

    RDAP is asked about the *apex*, because that is the only thing a registry has a record
    for: `shop.example.com` has no registration of its own. The row is still keyed by the
    domain as the listing named it, so the Ransomware table can join straight onto
    `victim_domain` without every query having to compute an apex first.
    """
    facts: dict[str, Any] = {"status": "not_scanned"}
    apex = apex_domain(domain) or domain

    rdap, dns, probe = await asyncio.gather(
        fetch_rdap(client, apex),
        resolve_domain(domain),
        probe_domain(client, domain),
        return_exceptions=True,
    )

    if not isinstance(rdap, BaseException) and not rdap.is_empty():
        facts["registrar"] = rdap.registrar
        facts["whois_contacts"] = rdap.contacts or None
        facts["registered_at"] = rdap.registered_at
        facts["expires_at"] = rdap.expires_at

    if not isinstance(dns, BaseException):
        facts["dns"] = {"a": dns.a, "aaaa": dns.aaaa, "mx": dns.mx, "ns": dns.ns}

    if isinstance(probe, BaseException):
        # The probe is what decides `status`, so when it is the thing that broke, say so
        # rather than leaving the row claiming "not_scanned" forever and being picked up
        # again on every future sweep.
        facts["status"] = "error"
        facts["error"] = f"{type(probe).__name__}: {probe}"[:300]
    else:
        facts["status"] = probe.status
        facts["http_status"] = probe.http_status
        facts["technologies"] = probe.technologies
        facts["page_title"] = probe.page_title
        facts["error"] = probe.error

    return facts


async def sweep_domains(
    *,
    storage: Storage,
    settings: Settings,
    limit: int | None = None,
) -> SweepResult:
    """Enrich one batch of the domains that most need it."""
    batch = limit if limit is not None else settings.enrich_batch_size
    domains = await storage.domains_needing_enrichment(
        limit=batch, max_age_seconds=settings.enrich_max_age_seconds
    )
    if not domains:
        return SweepResult()

    result = SweepResult(attempted=len(domains))
    # A ceiling on how many third-party servers we are talking to at once. This is the whole
    # politeness budget of the sweep — everything else about it is a consequence of this
    # number and how often the cron fires.
    slots = asyncio.Semaphore(max(1, settings.enrich_concurrency))

    async with enrich_client() as client:

        async def one(domain: str) -> None:
            async with slots:
                try:
                    facts = await _enrich_one(client, domain)
                    await storage.upsert_domain_enrichment(domain, facts)
                    result.enriched += 1
                except Exception as exc:  # noqa: BLE001 - one domain must not end the batch
                    result.failed += 1
                    log.warning("enrich sweep: domain failed", domain=domain, error=str(exc))

        await asyncio.gather(*(one(domain) for domain in domains))

    log.info(
        "enrich sweep finished",
        attempted=result.attempted,
        enriched=result.enriched,
        failed=result.failed,
    )
    return result
