"""URL identity and the allowlist. Pure functions, no database."""

from __future__ import annotations

import pytest

from intel.crawl.frontier import is_allowed, normalize_crawl_url

HOST = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"


def test_spellings_of_one_page_normalize_together() -> None:
    variants = [
        "https://example.com/page",
        "https://example.com/page/",
        "https://EXAMPLE.com/page",
        "https://example.com:443/page",
        "https://example.com/page#top",
        "https://example.com/page?utm_source=test",
        "https://example.com/page?utm_source=a&utm_medium=b&fbclid=zzz",
    ]
    assert len({normalize_crawl_url(u) for u in variants}) == 1


def test_real_query_parameters_are_kept() -> None:
    assert normalize_crawl_url("http://x.onion/list?page=2") != normalize_crawl_url(
        "http://x.onion/list?page=3"
    )
    assert normalize_crawl_url("http://x.onion/v?id=7") == "http://x.onion/v?id=7"


def test_parameter_order_does_not_change_identity() -> None:
    assert normalize_crawl_url("http://x.onion/l?b=2&a=1") == normalize_crawl_url(
        "http://x.onion/l?a=1&b=2"
    )


def test_tracking_is_dropped_but_a_real_parameter_beside_it_survives() -> None:
    assert normalize_crawl_url("http://x.onion/l?page=2&utm_campaign=c") == (
        "http://x.onion/l?page=2"
    )


def test_a_different_port_is_a_different_page() -> None:
    assert normalize_crawl_url("http://x.onion:8080/a") != normalize_crawl_url("http://x.onion/a")


def test_the_root_stays_a_root() -> None:
    assert normalize_crawl_url("http://x.onion") == normalize_crawl_url("http://x.onion/")


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/",
        f"http://{HOST}/victim/1",
        f"https://{HOST}/page?id=3",
        f"http://{HOST.upper()}/x",
    ],
)
def test_the_sources_own_host_is_allowed(url: str) -> None:
    assert is_allowed(url, allowed_hosts={HOST})


@pytest.mark.parametrize(
    "url",
    [
        "http://other.onion/",
        f"http://{HOST}.evil.com/",
        f"http://{HOST}@evil.com/",  # userinfo trick: the host is evil.com
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:5432/",
        "http://localhost/",
        "http://10.0.0.5/admin",
        "ftp://" + HOST + "/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
    ],
)
def test_nothing_else_is_allowed(url: str) -> None:
    assert not is_allowed(url, allowed_hosts={HOST})
