"""Passive enrichment: what can be learned about a domain without touching it much.

Five lookups, all of them things a browser or a public API answers:

    domains      normalize what a person typed into a domain worth looking up
    rdap         registrar and contact emails, from the registry
    dns_records  where the name points
    certs        hostnames certified under it, from certificate transparency logs
    probe        one GET of the home page: is it up, what is it built on

Nothing here scans, enumerates paths, or sends more than one request per target. That is a
deliberate boundary and not a limitation to be relaxed later: this is a research console
that reports on victims, and there is no version of "check whether the victim is patched"
that stays on the right side of the line.
"""

from .certs import CertFacts, fetch_certificates
from .dns_records import DnsFacts, resolve_domain
from .domains import apex_domain, looks_like_domain, normalize_domain, normalize_query
from .http import enrich_client
from .probe import ProbeResult, probe_domain
from .rdap import RdapFacts, fetch_rdap

__all__ = [
    "CertFacts",
    "DnsFacts",
    "ProbeResult",
    "RdapFacts",
    "apex_domain",
    "enrich_client",
    "fetch_certificates",
    "fetch_rdap",
    "looks_like_domain",
    "normalize_domain",
    "normalize_query",
    "probe_domain",
    "resolve_domain",
]
