"""Enrichment tests.

Everything here runs offline. The enrichers talk to registries, CT logs and victims' own
servers, and a test suite that depends on any of those is a suite that fails on a train — so
the network-shaped parts are exercised through a stub transport, and the parsing and
fingerprinting logic (which is where the bugs actually are) is tested directly.
"""

from __future__ import annotations

import httpx
import pytest

from intel.enrich.domains import (
    apex_domain,
    looks_like_domain,
    normalize_domain,
    normalize_query,
)
from intel.enrich.probe import ProbeResult, probe_domain
from intel.enrich.rdap import fetch_rdap

# --- domain normalization -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://WWW.Framegroup.com.au/about?x=1", "framegroup.com.au"),
        ("http://example.com", "example.com"),
        ("example.com.", "example.com"),
        ("user@host.example.org:8443", "host.example.org"),
        ("  Example.COM  ", "example.com"),
        # Not hostnames.
        ("not a domain", None),
        ("", None),
        ("localhost", None),
        ("double..dot.com", None),
        ("-leading.example.com", None),
    ],
)
def test_normalize_domain(raw: str, expected: str | None) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Two-label public suffixes: the apex is three labels deep, not two.
        ("mail.corp.framegroup.com.au", "framegroup.com.au"),
        ("framegroup.com.au", "framegroup.com.au"),
        ("shop.example.co.uk", "example.co.uk"),
        # Ordinary TLDs.
        ("shop.example.com", "example.com"),
        ("example.com", "example.com"),
        ("a.b.c.example.org", "example.org"),
    ],
)
def test_apex_domain(raw: str, expected: str) -> None:
    assert apex_domain(raw) == expected


def test_normalize_query_is_a_stable_cache_key() -> None:
    """Two spellings of one search must not run the same six network lookups twice."""
    assert normalize_query("  Frame   Group ") == normalize_query("frame group") == "frame group"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("framegroup.com.au", True), ("example.com", True), ("The Frame Group", False)],
)
def test_looks_like_domain(raw: str, expected: bool) -> None:
    assert looks_like_domain(raw) is expected


# --- fingerprinting -------------------------------------------------------------------


def _stub_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _probe_html(html: str, headers: dict[str, str] | None = None) -> ProbeResult:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=html, headers=headers or {})

    async with _stub_client(handler) as client:
        return await probe_domain(client, "example.com")


async def test_fingerprint_does_not_report_magento_for_an_image_tag() -> None:
    """Regression: the Magento rule was `mage/`, which matches inside `image/`.

    Every site on the internet serves an image, so every probed site came back tagged
    Magento — and a wrong technology chip is worse than a missing one, because someone acts
    on it. Kept as a test because the obvious short pattern is the wrong one.
    """
    result = await _probe_html('<html><body><img src="/images/logo.png"></body></html>')
    assert "Magento" not in result.technologies


async def test_fingerprint_does_not_report_a_product_that_is_merely_mentioned() -> None:
    """Prose about a CMS is not evidence the CMS is installed."""
    result = await _probe_html(
        "<html><body><p>We migrated from Joomla to WooCommerce "
        "and love Elementor.</p></body></html>"
    )
    assert result.technologies == []


async def test_fingerprint_detects_a_real_wordpress_stack() -> None:
    result = await _probe_html(
        """<html><head>
        <link rel="stylesheet" href="/wp-content/themes/x/style.css">
        <script src="/wp-includes/js/jquery/jquery.min.js"></script>
        <link rel="stylesheet" href="/wp-content/plugins/elementor/assets/css/frontend.css">
        <meta property="og:title" content="Home">
        </head><body></body></html>""",
        headers={"Server": "nginx/1.24.0", "X-Powered-By": "PHP/8.2.1"},
    )
    assert set(result.technologies) >= {
        "WordPress",
        "jQuery",
        "Elementor",
        "Nginx",
        "PHP",
        "Open Graph",
    }


async def test_probe_reports_a_5xx_as_down_but_a_403_as_live() -> None:
    """A 403 means something is listening. A 502 means it is not serving."""

    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    async with _stub_client(forbidden) as client:
        assert (await probe_domain(client, "example.com")).status == "live"
    async with _stub_client(broken) as client:
        assert (await probe_domain(client, "example.com")).status == "down"


async def test_probe_separates_an_unreachable_site_from_a_broken_probe() -> None:
    """`down` is evidence about the victim; `error` is evidence about us."""

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def protocol_error(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("malformed response", request=request)

    async with _stub_client(refused) as client:
        assert (await probe_domain(client, "example.com")).status == "down"
    async with _stub_client(protocol_error) as client:
        assert (await probe_domain(client, "example.com")).status == "error"


# --- RDAP ------------------------------------------------------------------------------

# A registry response trimmed to the parts the parser reads. The abuse contact is nested
# inside the registrar entity, which is exactly the shape a flat parse would miss.
_RDAP_RESPONSE = {
    "events": [
        {"eventAction": "registration", "eventDate": "2001-01-13T00:12:14Z"},
        {"eventAction": "expiration", "eventDate": "2031-01-13T00:12:14Z"},
    ],
    "status": ["client transfer prohibited"],
    "entities": [
        {
            "roles": ["registrar"],
            "vcardArray": [
                "vcard",
                [["version", {}, "text", "4.0"], ["fn", {}, "text", "Example Registrar LLC"]],
            ],
            "entities": [
                {
                    "roles": ["abuse"],
                    "vcardArray": [
                        "vcard",
                        [
                            ["version", {}, "text", "4.0"],
                            ["email", {}, "text", "Abuse@Registrar.example"],
                        ],
                    ],
                }
            ],
        },
        {
            "roles": ["registrant"],
            "vcardArray": [
                "vcard",
                [["version", {}, "text", "4.0"], ["email", {}, "text", "owner@victim.example"]],
            ],
        },
    ],
}


async def test_fetch_rdap_finds_the_nested_registrar_abuse_contact() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_RDAP_RESPONSE)

    async with _stub_client(handler) as client:
        facts = await fetch_rdap(client, "victim.example")

    assert facts.registrar == "Example Registrar LLC"
    # Lowercased, so the same address from two registries dedupes.
    assert facts.contacts["registrarAbuse"] == ["abuse@registrar.example"]
    assert facts.contacts["registrant"] == ["owner@victim.example"]
    assert facts.registered_at is not None
    assert facts.registered_at.year == 2001
    assert facts.expires_at is not None


async def test_fetch_rdap_treats_a_missing_record_as_empty_not_an_error() -> None:
    """Plenty of ccTLDs run no RDAP service. That must not fail a hunt that found leaks."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _stub_client(handler) as client:
        facts = await fetch_rdap(client, "victim.example")

    assert facts.is_empty()


async def test_fetch_rdap_survives_a_malformed_vcard() -> None:
    """A registry serving a broken card must not take down the whole lookup."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entities": [{"roles": ["registrant"], "vcardArray": "not-an-array"}]},
        )

    async with _stub_client(handler) as client:
        facts = await fetch_rdap(client, "victim.example")

    assert facts.is_empty()
