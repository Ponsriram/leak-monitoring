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

**Indicator hosts get WHOIS and nothing else.** The IOC table's WHOIS column needs the
registration record, which RDAP answers from the *registry*. The DNS lookup and the home-page
GET that victim domains get are skipped on purpose: an indicator host is live malware, phishing
or C2 infrastructure — URLhaus hosts serve payloads — and fetching from it, or resolving it
through its own name servers, would put this machine in contact with the attacker's side.
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
    ioc_hosts: int = 0
    ioc_lookups: int = 0


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
    """Enrich one batch of the victim domains, then one batch of indicator hosts."""
    batch = limit if limit is not None else settings.enrich_batch_size
    domains = await storage.domains_needing_enrichment(
        limit=batch, max_age_seconds=settings.enrich_max_age_seconds
    )
    hosts = await storage.ioc_hosts_needing_whois(
        limit=settings.enrich_ioc_batch_size,
        max_age_seconds=settings.enrich_ioc_max_age_seconds,
    )
    if not domains and not hosts:
        return SweepResult()

    result = SweepResult(attempted=len(domains), ioc_hosts=len(hosts))
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

        # Indicator hosts: RDAP only, one lookup per registrable domain. Feeds are full of
        # subdomains on one apex — a hundred `*.workers.dev` hosts are one registration —
        # so grouping is both the correct answer and a hundredfold saving on the registry.
        by_apex: dict[str, list[str]] = {}
        for host in hosts:
            by_apex.setdefault(apex_domain(host) or host, []).append(host)

        async def whois(apex: str, members: list[str]) -> None:
            async with slots:
                try:
                    rdap = await fetch_rdap(client, apex)
                except Exception as exc:  # noqa: BLE001 - a registry outage is recorded
                    facts: dict[str, Any] = {"error": f"rdap: {type(exc).__name__}"[:300]}
                else:
                    result.ioc_lookups += 1
                    facts = {}
                    if not rdap.is_empty():
                        facts = {
                            "registrar": rdap.registrar,
                            "whois_contacts": rdap.contacts or None,
                            "registered_at": rdap.registered_at,
                            "expires_at": rdap.expires_at,
                        }
                # `status` stays not_scanned: nothing here looked at the site, and saying
                # otherwise would put a claim about a host we deliberately never touched
                # into the site-status column.
                facts["status"] = "not_scanned"
                for host in members:
                    await storage.upsert_domain_enrichment(host, facts)

        await asyncio.gather(*(whois(apex, members) for apex, members in by_apex.items()))

    log.info(
        "enrich sweep finished",
        attempted=result.attempted,
        enriched=result.enriched,
        failed=result.failed,
        ioc_hosts=result.ioc_hosts,
        ioc_lookups=result.ioc_lookups,
    )
    return result
