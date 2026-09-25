"""`intel sources probe` checks, on page shapes seen on real candidate sites."""

from __future__ import annotations

from intel.probe import evaluate, yaml_snippet

URL = "http://" + "a" * 56 + ".onion/leaks"

LISTING = (
    "<body><nav><a href='/leaks?page=2'>2</a></nav>"
    + "".join(
        f"<div><h2>{name} Logistics</h2><p>Website: {name.lower()}.com</p>"
        f"<p>Published: 2026-09-{n + 1:02d}</p><p>Size: 120 GB</p></div>"
        for n, name in enumerate(
            [
                "Northwind",
                "Contoso",
                "Fabrikam",
                "Tailspin",
                "Litware",
                "Adatum",
                "Proseware",
                "Wingtip",
            ]
        )
    )
    + "</body>"
)


def test_a_readable_listing_passes() -> None:
    report = evaluate(URL, "http", LISTING, None)
    assert report.usable
    assert report.problems() == []
    assert report.pagination == "query"


def test_unreachable() -> None:
    report = evaluate(URL, "http", None, "HTTP 403")
    assert not report.usable
    assert report.problems() == ["unreachable: HTTP 403"]


def test_a_gate_page_fails_as_a_gate() -> None:
    html = "<body><p>Blackout Gateway</p><p>We are checking that you are human</p></body>"
    report = evaluate(URL, "http", html, None)
    assert not report.usable
    assert "human check" in report.problems()[0]


def test_a_js_shell_fails_as_near_empty() -> None:
    report = evaluate(URL, "http", "<body><div id='root'></div><script>x()</script></body>", None)
    assert "near-empty" in report.problems()[0]


def test_a_forum_is_flagged_even_when_names_extract() -> None:
    html = (
        "<body><p>Forums</p><p>New posts</p><p>All threads</p><p>Latest threads</p>"
        + LISTING.removeprefix("<body>")
    )
    report = evaluate(URL, "http", html, None)
    assert not report.usable
    assert any("forum/market/shop" in p for p in report.problems())


def test_a_news_page_points_at_the_listing_link() -> None:
    # lynx: /leak is press releases; the list lives at /leaks.
    url = "http://" + "b" * 56 + ".onion/leak"
    html = (
        "<body><a href='/'>News</a><a href='/leaks'>Leaks</a>"
        "<p>24/07/2024 Press Release: our motivation is financial.</p></body>"
    )
    report = evaluate(url, "http", html, None)
    assert not report.usable
    assert "http://" + "b" * 56 + ".onion/leaks" in report.listing_links


def test_an_announced_move_is_reported() -> None:
    new = "c" * 56 + ".onion"
    html = f"<body><p>We moved to a new address: {new}</p></body>"
    report = evaluate(URL, "http", html, None)
    assert report.announced_mirrors == [f"http://{new}"]


def test_a_move_notice_in_other_words_still_shows_the_address() -> None:
    # z3wqggtxft7i's actual notice, which none of the announcement patterns match.
    new = "d" * 56 + ".onion"
    html = f"<body><p>Migration completed</p><p>Blog is available now - {new}</p></body>"
    report = evaluate(URL, "http", html, None)
    assert report.other_onions == [f"http://{new}"]


def test_yaml_snippet_ships_disabled() -> None:
    snippet = yaml_snippet(URL, "browser", "path")
    assert "collector: browser" in snippet
    assert "pagination_style: path" in snippet
    assert snippet.endswith("enabled: false")
