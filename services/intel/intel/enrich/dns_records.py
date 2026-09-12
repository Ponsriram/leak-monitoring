"""Where a domain points.

Address records come from the standard library, which every deployment has. Mail and
nameserver records need a real resolver, and `dnspython` is an optional dependency — so this
module returns what it can get and reports what it could not, rather than failing whole.

That split is not laziness. `A` records decide whether the liveness probe is worth making at
all, so they must never depend on an extra package being installed; `MX` and `NS` are context
an analyst likes to have and can do without.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

__all__ = ["DnsFacts", "resolve_domain"]

try:  # pragma: no cover - import-time branch, decided by whether the extra is installed
    import dns.asyncresolver
    import dns.resolver

    _HAS_DNSPYTHON = True
except ImportError:  # pragma: no cover
    _HAS_DNSPYTHON = False

# One resolver's worth of patience. A hunt runs this alongside three other lookups while a
# person waits, and a domain whose nameservers are gone should report "gone" quickly.
_DNS_TIMEOUT = 5.0


@dataclass(slots=True)
class DnsFacts:
    a: list[str] = field(default_factory=list)
    aaaa: list[str] = field(default_factory=list)
    mx: list[str] = field(default_factory=list)
    ns: list[str] = field(default_factory=list)
    # Set when the name does not resolve at all. Distinct from "resolved to nothing".
    nxdomain: bool = False
    # Which record types could not be looked up, and why. Keeps `partial` honest.
    unavailable: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.a or self.aaaa or self.mx or self.ns)


async def _addresses(domain: str) -> tuple[list[str], list[str], bool]:
    """A and AAAA via `getaddrinfo`, off the event loop thread.

    `getaddrinfo` is a blocking C call. Awaiting it directly would stall every other
    coroutine in the worker — including the other enrichers in this same hunt — for as long
    as the system resolver takes, which on a dead domain is seconds.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # NXDOMAIN and "no address associated" are both normal answers about a dead site.
        log.debug("dns: no address", domain=domain, error=str(exc))
        return [], [], True
    except OSError as exc:
        log.debug("dns: resolver error", domain=domain, error=str(exc))
        return [], [], False

    v4: list[str] = []
    v6: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if family == socket.AF_INET and address not in v4:
            v4.append(address)
        elif family == socket.AF_INET6 and address not in v6:
            v6.append(address)
    return v4, v6, False


async def _records(domain: str, record_type: str) -> tuple[list[str], str | None]:
    """One record type via dnspython. Returns (values, unavailable_reason)."""
    if not _HAS_DNSPYTHON:
        return [], "dnspython is not installed"

    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = _DNS_TIMEOUT
    resolver.lifetime = _DNS_TIMEOUT

    try:
        answer = await resolver.resolve(domain, record_type)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        # "This domain has no MX record" is a complete answer, not a failure.
        return [], None
    except Exception as exc:  # noqa: BLE001 - dnspython raises a wide tree; none is fatal here
        return [], f"{type(exc).__name__}: {exc}"[:200]

    values: list[str] = []
    for record in answer:
        value = record.to_text().rstrip(".")
        if value and value not in values:
            values.append(value)
    return values, None


async def resolve_domain(domain: str) -> DnsFacts:
    """Everything DNS will tell us about a domain, looked up concurrently."""
    addresses, mx, ns = await asyncio.gather(
        _addresses(domain),
        _records(domain, "MX"),
        _records(domain, "NS"),
    )

    v4, v6, nxdomain = addresses
    mx_values, mx_error = mx
    ns_values, ns_error = ns

    facts = DnsFacts(a=v4, aaaa=v6, mx=mx_values, ns=ns_values, nxdomain=nxdomain)
    if mx_error:
        facts.unavailable["MX"] = mx_error
    if ns_error:
        facts.unavailable["NS"] = ns_error
    return facts
