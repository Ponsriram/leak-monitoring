"""The background enrichment sweep, with the network and database faked out."""

from __future__ import annotations

from typing import Any

import pytest

from intel import enrich_sweep
from intel.config import get_settings
from intel.enrich.rdap import RdapFacts


class FakeStorage:
    def __init__(self, domains: list[str], hosts: list[str]) -> None:
        self.domains = domains
        self.hosts = hosts
        self.written: dict[str, dict[str, Any]] = {}

    async def domains_needing_enrichment(self, *, limit: int, max_age_seconds: int) -> list[str]:
        return self.domains[:limit]

    async def ioc_hosts_needing_whois(self, *, limit: int, max_age_seconds: int) -> list[str]:
        return self.hosts[:limit]

    async def upsert_domain_enrichment(self, domain: str, facts: dict[str, Any]) -> None:
        self.written[domain] = facts


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"rdap": [], "probe": [], "dns": []}

    async def fake_rdap(_client: object, domain: str) -> RdapFacts:
        seen["rdap"].append(domain)
        return RdapFacts(registrar="Example Registrar", contacts={"registrarAbuse": ["a@b.co"]})

    async def fake_probe(_client: object, domain: str) -> object:
        seen["probe"].append(domain)
        raise RuntimeError("not under test")

    async def fake_dns(domain: str) -> object:
        seen["dns"].append(domain)
        raise RuntimeError("not under test")

    monkeypatch.setattr(enrich_sweep, "fetch_rdap", fake_rdap)
    monkeypatch.setattr(enrich_sweep, "probe_domain", fake_probe)
    monkeypatch.setattr(enrich_sweep, "resolve_domain", fake_dns)
    return seen


async def test_indicator_hosts_get_whois_without_being_contacted(
    calls: dict[str, list[str]],
) -> None:
    """An IOC host is attacker infrastructure: RDAP asks the registry, nothing asks the host."""
    storage = FakeStorage(domains=[], hosts=["evil.example.com", "cdn.evil.example.com"])

    result = await enrich_sweep.sweep_domains(storage=storage, settings=get_settings())  # type: ignore[arg-type]

    assert calls["probe"] == []
    assert calls["dns"] == []
    assert calls["rdap"] == ["example.com"], "one lookup per registrable domain"
    assert result.ioc_hosts == 2
    for host in ("evil.example.com", "cdn.evil.example.com"):
        assert storage.written[host]["registrar"] == "Example Registrar"
        assert storage.written[host]["status"] == "not_scanned"


async def test_victim_domains_still_get_the_full_enrichment(
    calls: dict[str, list[str]],
) -> None:
    storage = FakeStorage(domains=["victim.example.org"], hosts=[])

    await enrich_sweep.sweep_domains(storage=storage, settings=get_settings())  # type: ignore[arg-type]

    assert calls["probe"] == ["victim.example.org"]
    assert calls["dns"] == ["victim.example.org"]
