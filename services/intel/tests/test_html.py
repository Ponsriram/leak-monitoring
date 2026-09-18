"""HTML → text: volatile widgets go, the listing around them stays."""

from __future__ import annotations

from intel.collectors import to_text


def test_a_countdown_widget_is_dropped() -> None:
    html = '<body><p>Acme Corp</p><div class="countdown-timer">2h 39m</div></body>'
    text = to_text(html)
    assert "Acme Corp" in text
    assert "2h 39m" not in text


def test_a_card_whose_class_mentions_countdown_is_kept() -> None:
    # The shape of direwolf's listing: the timer is dropped, the victim card is not.
    card = (
        '<div class="card countdown-active">'
        "<h2>Aztec Software</h2><span>Website: https://www.aztecsoftware.com</span>"
        "<span>Industry: Engineering Software</span>"
        '<div class="countdown-timer active">2h 39m</div>'
        "</div>"
    )
    text = to_text(f"<body>{card * 3}</body>")
    assert text.count("Aztec Software") == 3
    assert "2h 39m" not in text
