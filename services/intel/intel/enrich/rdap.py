"""Registration facts, from RDAP.

RDAP is WHOIS's replacement: the same data, but JSON over HTTPS with a documented schema
instead of free-form text that every registry formats differently. That matters more here
than it sounds — getting a registrant email out of classic WHOIS means a regex over prose
whose shape changes per TLD, and those regexes break silently and constantly.

This is what fills the WHOIS column: registrar, contact emails by role, and the registration
and expiry dates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

__all__ = ["RdapFacts", "fetch_rdap"]

# Bootstrap. Every TLD's records live on its own registry's server, and `rdap.org` is the
# redirector that knows which — one request instead of parsing IANA's bootstrap file and
# maintaining a TLD-to-endpoint map here.
_RDAP_BOOTSTRAP = "https://rdap.org/domain/{domain}"

# RDAP role -> the bucket the UI groups contacts under.
#
# `administrative` and `technical` are the spec's spellings; the column headings say ADMIN
# and TECH. Roles outside this map (`reseller`, `sponsor`, `noc`) are dropped rather than
# bucketed as "other" — a chip labelled with a role nobody recognises is noise.
_ROLE_BUCKETS = {
    "registrant": "registrant",
    "administrative": "admin",
    "technical": "tech",
    "billing": "billing",
    "abuse": "registrarAbuse",
}


@dataclass(slots=True)
class RdapFacts:
    """What one RDAP lookup yielded. Every field is optional — registries redact freely."""

    registrar: str | None = None
    contacts: dict[str, list[str]] = field(default_factory=dict)
    registered_at: datetime | None = None
    expires_at: datetime | None = None
    # Registry status flags: `clientTransferProhibited`, `redemptionPeriod`, and so on.
    # `redemptionPeriod` in particular means the domain is being deleted, which is worth
    # knowing about a company that was just listed on a leak site.
    statuses: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.registrar or self.contacts or self.registered_at)


def _vcard_emails(entity: dict[str, Any]) -> list[str]:
    """Pull email addresses out of an entity's jCard.

    jCard is JSON-encoded vCard, and its shape is genuinely awkward: `["vcard", [[name,
    params, type, value], ...]]`. Each property is a positional array, so the value is
    always index 3 and there is no key to look it up by. Guarded at every step because a
    registry returning a malformed card must not take the whole hunt down.
    """
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2 or not isinstance(card[1], list):
        return []

    emails: list[str] = []
    for prop in card[1]:
        if not isinstance(prop, list) or len(prop) < 4:
            continue
        if prop[0] != "email":
            continue
        value = prop[3]
        if isinstance(value, str) and "@" in value:
            emails.append(value.strip().lower())
    return emails


def _vcard_name(entity: dict[str, Any]) -> str | None:
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2 or not isinstance(card[1], list):
        return None
    for prop in card[1]:
        if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
            value = prop[3]
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _collect_entities(
    entities: Any,
    facts: RdapFacts,
    *,
    depth: int = 0,
) -> None:
    """Walk the entity tree, filing emails under the role that carries them.

    Recursive because the registrar's abuse contact is nested *inside* the registrar entity
    rather than sitting alongside it — which is exactly the address anyone reporting a
    compromised site needs, so a flat pass over the top level would miss the single most
    useful email in the record. Depth-capped because the schema does not forbid a cycle.
    """
    if depth > 3 or not isinstance(entities, list):
        return

    for entity in entities:
        if not isinstance(entity, dict):
            continue

        roles = entity.get("roles")
        roles = [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []

        if "registrar" in roles and facts.registrar is None:
            facts.registrar = _vcard_name(entity)

        emails = _vcard_emails(entity)
        if emails:
            for role in roles:
                bucket = _ROLE_BUCKETS.get(role)
                if bucket is None:
                    continue
                existing = facts.contacts.setdefault(bucket, [])
                for email in emails:
                    if email not in existing:
                        existing.append(email)

        _collect_entities(entity.get("entities"), facts, depth=depth + 1)


def _event_date(events: Any, action: str) -> datetime | None:
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict) or event.get("eventAction") != action:
            continue
        raw = event.get("eventDate")
        if not isinstance(raw, str):
            continue
        try:
            # RDAP dates are RFC 3339. `fromisoformat` handles the trailing Z from 3.11 on.
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            log.debug("rdap: unparseable event date", action=action, raw=raw)
    return None


async def fetch_rdap(client: httpx.AsyncClient, domain: str) -> RdapFacts:
    """Look up one domain. Returns empty facts rather than raising, for anything expected.

    A 404 is the normal answer for an unregistered domain and for the many ccTLDs that run
    no RDAP service at all — that is information, not an error, and it must not turn a hunt
    that found leak matches into a failed hunt. Genuine transport failures propagate, so the
    orchestrator can record *which* enricher was unavailable.
    """
    response = await client.get(
        _RDAP_BOOTSTRAP.format(domain=domain),
        headers={"Accept": "application/rdap+json, application/json"},
    )

    if response.status_code == 404:
        log.debug("rdap: no record", domain=domain)
        return RdapFacts()
    if response.status_code >= 400:
        log.debug("rdap: lookup failed", domain=domain, status=response.status_code)
        return RdapFacts()

    try:
        payload = response.json()
    except ValueError:
        log.debug("rdap: non-json response", domain=domain)
        return RdapFacts()

    if not isinstance(payload, dict):
        return RdapFacts()

    facts = RdapFacts()
    _collect_entities(payload.get("entities"), facts)

    events = payload.get("events")
    facts.registered_at = _event_date(events, "registration")
    facts.expires_at = _event_date(events, "expiration")

    statuses = payload.get("status")
    if isinstance(statuses, list):
        facts.statuses = [s for s in statuses if isinstance(s, str)]

    return facts
