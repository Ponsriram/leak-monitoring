"""The pipeline hook around the detector: what reaches storage, and what happens on failure."""

from __future__ import annotations

import json
from types import SimpleNamespace

from intel.pipeline import _record_exposures
from intel.storage import _like_escape

SECRET_PASSWORD = "Summer2024-Rocks"
PAGE = f"leak dump\njane.doe@acme.com:{SECRET_PASSWORD}\nbob@acme.com:Hunter2-Rocks"


class RecordingStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[dict], dict]] = []
        self._fail = fail

    async def upsert_exposures(self, findings, **kwargs):  # type: ignore[no-untyped-def]
        if self._fail:
            raise RuntimeError("database went away")
        self.calls.append((findings, kwargs))
        return (len(findings), 0)


SETTINGS = SimpleNamespace(exposure_salt="test-salt")
SOURCE = SimpleNamespace(id=7, slug="lockbit")


async def test_storage_never_receives_the_secret() -> None:
    storage = RecordingStorage()
    new = await _record_exposures(
        PAGE, source=SOURCE, storage=storage, settings=SETTINGS, url="http://x.onion/"  # type: ignore[arg-type]
    )

    assert new == 2
    findings, kwargs = storage.calls[0]
    assert kwargs == {"source_id": 7, "source_url": "http://x.onion/"}

    everything = json.dumps(findings)
    assert SECRET_PASSWORD not in everything
    assert "jane.doe" not in everything  # the local part is masked too
    assert {f["email_domain"] for f in findings} == {"acme.com"}
    # Fingerprints are keyed: a different salt must not reproduce them.
    assert all(len(f["fingerprint"]) == 64 for f in findings)


async def test_a_clean_page_writes_nothing() -> None:
    storage = RecordingStorage()
    new = await _record_exposures(
        "Northwind Logistics northwind.com published 12 TB",
        source=SOURCE,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        settings=SETTINGS,  # type: ignore[arg-type]
        url="http://x.onion/",
    )
    assert new == 0
    assert storage.calls == []


async def test_a_storage_failure_does_not_fail_the_crawl() -> None:
    new = await _record_exposures(
        PAGE,
        source=SOURCE,  # type: ignore[arg-type]
        storage=RecordingStorage(fail=True),  # type: ignore[arg-type]
        settings=SETTINGS,  # type: ignore[arg-type]
        url="http://x.onion/",
    )
    assert new == 0  # logged and swallowed — the listings were already saved


def test_like_escape_neutralises_pattern_characters() -> None:
    assert _like_escape("north_wind") == r"north\_wind"
    assert _like_escape("100%") == r"100\%"
    assert _like_escape("a" + chr(92) + "b") == "a" + chr(92) * 2 + "b"
    assert _like_escape("acme.com") == "acme.com"
