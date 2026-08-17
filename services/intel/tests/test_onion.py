"""Onion address discovery.

The point of this module is picking a site's announced replacement address out of a page.
The tests that matter are the ones about what it refuses to return, because every address
it does return is a candidate for the crawler to connect to.
"""

from __future__ import annotations

from intel.collectors.onion import classify_onion_urls, find_onion_urls, onion_host

SELF = "akiralkzxzq2dsrzsrvbr2xgbbu2wgsmxryd4csgfameg52n7efvr2id.onion"
OTHER = "lockbitapt2yfbt7lchxejug47kmqvqqxvvjpqkmevv4l3azl3gy6pyd.onion"


def test_finds_a_bare_address() -> None:
    found = find_onion_urls(f"our new address is {OTHER} — update your bookmarks")
    assert found == {OTHER: f"http://{OTHER}"}


def test_keeps_scheme_and_path_when_the_page_gives_them() -> None:
    found = find_onion_urls(f"mirror: https://{OTHER}/blog/disclosure")
    assert found[OTHER] == f"https://{OTHER}/blog/disclosure"


def test_excluded_hosts_are_dropped() -> None:
    """A source's own address is not a mirror of itself."""
    text = f"you are here: {SELF}. our mirror: {OTHER}"
    assert set(find_onion_urls(text, exclude_hosts={SELF})) == {OTHER}


def test_repeats_collapse_to_one_candidate() -> None:
    text = f"header {OTHER} ... footer http://{OTHER}/blog"
    assert len(find_onion_urls(text)) == 1


def test_v2_addresses_are_ignored() -> None:
    """16-character v2 addresses have been unroutable since 2021, and the shorter pattern
    matches a lot of ordinary base32-looking noise."""
    assert find_onion_urls("legacy at abcdefghij234567.onion") == {}


def test_case_is_normalized() -> None:
    assert list(find_onion_urls(f"MIRROR: {OTHER.upper()}")) == [OTHER]


def test_onion_host_pulls_the_host_out_of_a_url() -> None:
    assert onion_host(f"https://{OTHER}/blog?x=1") == OTHER
    assert onion_host("https://example.com/") is None


def test_only_announced_addresses_are_eligible_for_failover() -> None:
    """The security-relevant split.

    An address a page presents as its own mirror can be followed automatically; an address
    that merely appears on the page — a negotiation portal, another crew's site — must not
    be, or a crawled host could redirect the crawler anywhere by printing a link.
    """
    text = (
        f"Our new address is {OTHER} — update your bookmarks.\n\n"
        f"Chat with support: {SELF}"
    )
    announced, other = classify_onion_urls(text)
    assert set(announced) == {OTHER}
    assert set(other) == {SELF}


def test_an_unlabelled_address_is_never_announced() -> None:
    announced, other = classify_onion_urls(f"see also {OTHER}")
    assert announced == {}
    assert set(other) == {OTHER}


def test_a_list_of_mirrors_under_one_heading_is_all_announced() -> None:
    """The common footer shape: one heading, then addresses on their own lines."""
    third = "termiteuslbumdge2zmfmfcsrvmvsfe4gvyudc5j6cdnisnhtftvokidxxxxxxx"[:56] + ".onion"
    text = f"Our mirrors:\n\n{OTHER}\n{SELF}\n{third}\n"
    announced, other = classify_onion_urls(text)
    assert set(announced) == {OTHER, SELF, third}
    assert other == {}
