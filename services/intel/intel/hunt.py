"""Company search that goes and looks.

The instant half of search reads the index and returns in milliseconds. This is the other
half: someone types a company we hold little on, and the worker fans out to everything that
can say something about it.

Two properties shape the whole module.

**Findings are written as they arrive, not at the end.** Leak matches come from our own
database and land in about ten milliseconds; crt.sh can take fifteen seconds when it takes
any time at all. Waiting for the slowest lookup before showing the fastest would make the
common case — "we already had three listings for this company" — feel as slow as the rarest.

**No single lookup can fail the hunt.** Each enricher is isolated: a registry outage records
itself in `errors`, the hunt finishes `partial`, and everything else still shows. The old
instinct — gather() and let the first exception win — would mean crt.sh being down deletes
the WHOIS record we successfully fetched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from .enrich import (
    apex_domain,
    enrich_client,
    fetch_certificates,
    fetch_rdap,
    looks_like_domain,
    probe_domain,
    resolve_domain,
)
from .storage import HuntJobRow, Storage

log = structlog.get_logger(__name__)

__all__ = ["HuntOutcome", "run_hunt"]

# How many of each local match to record. A hunt is a summary, not an export — the leaks
# table is one click away and does pagination properly.
_MAX_LEAK_MATCHES = 25
_MAX_CORPUS_MATCHES = 10


@dataclass(slots=True)
class HuntOutcome:
    status: str
    target_domain: str | None = None
    findings: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _leak_finding(row: dict[str, Any]) -> dict[str, Any]:
    victim = row.get("victim_name") or row.get("victim_domain") or "unnamed victim"
    return {
        "kind": "leak_match",
        "title": f"{victim} listed by {row['actor_group']}",
        "source_label": row.get("source_slug") or "leaks",
        "occurred_at": row.get("first_seen_at"),
        "detail": {
            "leakId": row["id"],
            "victimName": row.get("victim_name"),
            "victimDomain": row.get("victim_domain"),
            "victimCountry": row.get("victim_country"),
            "victimSector": row.get("victim_sector"),
            "actorGroup": row["actor_group"],
            "status": row.get("status"),
            "sourceUrl": row.get("source_url"),
            "publishedAt": row["published_at"].isoformat() if row.get("published_at") else None,
            "firstSeenAt": row["first_seen_at"].isoformat() if row.get("first_seen_at") else None,
            "lastSeenAt": row["last_seen_at"].isoformat() if row.get("last_seen_at") else None,
            "leakSizeBytes": row.get("leak_size_bytes"),
        },
    }


def _corpus_finding(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "raw_page_mention",
        "title": f"Mentioned on {row.get('source_name') or row.get('source_slug')}",
        "source_label": "corpus",
        "occurred_at": row.get("fetched_at"),
        "detail": {
            "rawPageId": row["id"],
            "url": row.get("url"),
            "pageNo": row.get("page_no"),
            "sourceSlug": row.get("source_slug"),
            # Already delimited with << >> by ts_headline, so the UI can mark the term
            # without re-running the match client-side.
            "excerpt": row.get("excerpt"),
        },
    }


async def _resolve_target(
    storage: Storage, query: str, leak_rows: list[dict[str, Any]]
) -> str | None:
    """Work out which domain to enrich.

    A typed domain is taken at face value. A company name has no domain until something tells
    us one, and the most trustworthy source available is our own data: if a leak listing named
    this company and recorded their domain, that is a mapping a human curated upstream. The
    alternative — guessing `<company>.com` — is how you end up publishing the WHOIS record of
    an unrelated business that happens to own the obvious domain.
    """
    if looks_like_domain(query):
        return apex_domain(query)

    for row in leak_rows:
        candidate = apex_domain(row.get("victim_domain"))
        if candidate:
            return candidate
    return None


async def run_hunt(storage: Storage, job: HuntJobRow) -> HuntOutcome:
    """Run one hunt to completion, writing findings as they land."""
    query = job.query.strip()
    total = 0
    errors: dict[str, str] = {}

    bound = log.bind(hunt_id=job.id, query=query)
    bound.info("hunt started")

    # --- what we already know. Local, fast, and the reason to search at all. ---

    leak_rows: list[dict[str, Any]] = []
    try:
        leak_rows = await storage.find_leaks_for_query(query, limit=_MAX_LEAK_MATCHES)
        total += await storage.add_hunt_findings(job.id, [_leak_finding(r) for r in leak_rows])
    except Exception as exc:  # noqa: BLE001 - one source failing must not end the hunt
        errors["leaks"] = f"{type(exc).__name__}: {exc}"[:200]
        bound.warning("hunt: leak lookup failed", error=str(exc))

    try:
        corpus_rows = await storage.search_corpus(query, limit=_MAX_CORPUS_MATCHES)
        total += await storage.add_hunt_findings(job.id, [_corpus_finding(r) for r in corpus_rows])
    except Exception as exc:  # noqa: BLE001
        errors["corpus"] = f"{type(exc).__name__}: {exc}"[:200]
        bound.warning("hunt: corpus search failed", error=str(exc))

    target = await _resolve_target(storage, query, leak_rows)

    if target is None:
        # Not a failure. A company name we hold no domain for has nothing external to look
        # up, and saying so plainly beats reporting an error nobody can act on.
        bound.info("hunt finished", findings=total, target=None)
        status = "partial" if errors else "succeeded"
        return HuntOutcome(status=status, findings=total, errors=errors)

    bound = bound.bind(target=target)

    # --- what the outside world can tell us. Concurrent, isolated, each writing its own. ---

    facts: dict[str, Any] = {"status": "not_scanned"}

    async with enrich_client() as client:

        async def registration() -> None:
            rdap = await fetch_rdap(client, target)
            if rdap.is_empty():
                return
            facts["registrar"] = rdap.registrar
            facts["whois_contacts"] = rdap.contacts or None
            facts["registered_at"] = rdap.registered_at
            facts["expires_at"] = rdap.expires_at
            nonlocal total
            total += await storage.add_hunt_findings(
                job.id,
                [
                    {
                        "kind": "registration",
                        "title": f"Registered through {rdap.registrar}"
                        if rdap.registrar
                        else f"Registration record for {target}",
                        "source_label": "RDAP",
                        "occurred_at": rdap.registered_at,
                        "detail": {
                            "domain": target,
                            "registrar": rdap.registrar,
                            "contacts": rdap.contacts,
                            "registeredAt": rdap.registered_at.isoformat()
                            if rdap.registered_at
                            else None,
                            "expiresAt": rdap.expires_at.isoformat() if rdap.expires_at else None,
                            "statuses": rdap.statuses,
                        },
                    }
                ],
            )

        async def infrastructure() -> None:
            dns = await resolve_domain(target)
            facts["dns"] = {
                "a": dns.a,
                "aaaa": dns.aaaa,
                "mx": dns.mx,
                "ns": dns.ns,
            }
            if dns.unavailable:
                errors.update({f"dns:{k}": v for k, v in dns.unavailable.items()})
            if dns.is_empty() and not dns.nxdomain:
                return
            nonlocal total
            title = (
                f"{target} does not resolve"
                if dns.nxdomain
                else f"Resolves to {', '.join(dns.a[:3]) or 'IPv6 only'}"
            )
            total += await storage.add_hunt_findings(
                job.id,
                [
                    {
                        "kind": "infrastructure",
                        "title": title,
                        "source_label": "DNS",
                        "detail": {"domain": target, "nxdomain": dns.nxdomain, **facts["dns"]},
                    }
                ],
            )

        async def certificates() -> None:
            certs = await fetch_certificates(client, target)
            if certs.is_empty():
                return
            existing = facts.setdefault("dns", {})
            existing["subdomains"] = certs.subdomains
            nonlocal total
            total += await storage.add_hunt_findings(
                job.id,
                [
                    {
                        "kind": "certificate",
                        "title": f"{len(certs.subdomains)} certified hostname(s)"
                        + (" (truncated)" if certs.truncated else ""),
                        "source_label": "crt.sh",
                        "detail": {
                            "domain": target,
                            "subdomains": certs.subdomains,
                            "truncated": certs.truncated,
                        },
                    }
                ],
            )

        async def liveness() -> None:
            probe = await probe_domain(client, target)
            facts["status"] = probe.status
            facts["http_status"] = probe.http_status
            facts["technologies"] = probe.technologies
            facts["page_title"] = probe.page_title
            facts["error"] = probe.error
            nonlocal total
            total += await storage.add_hunt_findings(
                job.id,
                [
                    {
                        "kind": "liveness",
                        "title": f"Site is {probe.status}"
                        + (f" (HTTP {probe.http_status})" if probe.http_status else ""),
                        "source_label": "probe",
                        "detail": {
                            "domain": target,
                            "status": probe.status,
                            "httpStatus": probe.http_status,
                            "technologies": probe.technologies,
                            "pageTitle": probe.page_title,
                            "finalUrl": probe.final_url,
                            "error": probe.error,
                        },
                    }
                ],
            )

        enrichers = {
            "rdap": registration,
            "dns": infrastructure,
            "crt.sh": certificates,
            "probe": liveness,
        }

        # `return_exceptions=True` is the isolation. Without it the first enricher to raise
        # cancels the rest, and a crt.sh timeout would discard a WHOIS record we already had
        # in hand.
        results = await asyncio.gather(*(fn() for fn in enrichers.values()), return_exceptions=True)
        for name, result in zip(enrichers, results, strict=True):
            if isinstance(result, BaseException):
                errors[name] = f"{type(result).__name__}: {result}"[:200]
                bound.warning("hunt: enricher failed", enricher=name, error=str(result))

    # One cache row per domain, written once with everything the enrichers agreed on, so a
    # later table render gets WHOIS, technologies and status without repeating any of this.
    try:
        await storage.upsert_domain_enrichment(target, facts)
    except Exception as exc:  # noqa: BLE001
        errors["cache"] = f"{type(exc).__name__}: {exc}"[:200]
        bound.warning("hunt: enrichment cache write failed", error=str(exc))

    status = "partial" if errors else "succeeded"
    bound.info("hunt finished", findings=total, status=status, errors=sorted(errors))
    return HuntOutcome(status=status, target_domain=target, findings=total, errors=errors)
