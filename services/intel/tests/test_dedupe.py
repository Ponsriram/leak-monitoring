"""Dedupe-hash tests.

The old pipeline had no key at all, so every run re-inserted the whole dataset. Later, the
first version of the *seed script* reintroduced the same bug by folding a `now()`-derived
timestamp into its hash. These tests exist so it cannot come back a third time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from intel.models import ExtractedLeak, ExtractionMeta, ExtractionMethod

META = ExtractionMeta(method=ExtractionMethod.RULES)


def leak(**kwargs: object) -> ExtractedLeak:
    base: dict[str, object] = {
        "victim_name": "Northwind Logistics",
        "victim_domain": "northwind.example",
        "actor_group": "lockbit",
        "extraction": META,
    }
    base.update(kwargs)
    return ExtractedLeak(**base)  # type: ignore[arg-type]


def test_hash_is_stable_across_instances() -> None:
    assert leak().dedupe_hash == leak().dedupe_hash


def test_hash_ignores_timestamps() -> None:
    """The whole point. A hash that moves with the clock re-inserts everything each run."""
    a = leak(published_at=datetime(2026, 2, 10, tzinfo=UTC))
    b = leak(published_at=datetime(2026, 8, 1, tzinfo=UTC))
    assert a.dedupe_hash == b.dedupe_hash


def test_hash_ignores_mutable_attributes() -> None:
    """Status and size change as a listing progresses; identity does not."""
    a = leak(status="countdown", leak_size_bytes=1000)
    b = leak(status="published", leak_size_bytes=999_999)
    assert a.dedupe_hash == b.dedupe_hash


def test_different_victims_hash_differently() -> None:
    assert leak().dedupe_hash != leak(
        victim_name="Contoso", victim_domain="contoso.example"
    ).dedupe_hash


def test_same_victim_different_group_hashes_differently() -> None:
    """Two crews listing the same company are two separate leak events."""
    assert leak().dedupe_hash != leak(actor_group="blackcat").dedupe_hash


def test_group_case_does_not_split_identity() -> None:
    """"LockBit", "lockbit" and "LOCKBIT" are one actor, not three."""
    assert leak(actor_group="LockBit").dedupe_hash == leak(actor_group="lockbit").dedupe_hash


def test_domain_normalisation_does_not_split_identity() -> None:
    assert (
        leak(victim_domain="https://www.northwind.example/").dedupe_hash
        == leak(victim_domain="northwind.example").dedupe_hash
    )


def test_domain_preferred_over_name_for_identity() -> None:
    """A site may render the name differently across pages; the domain is stabler."""
    a = leak(victim_name="Northwind Logistics")
    b = leak(victim_name="Northwind Logistics Inc.")
    assert a.dedupe_hash == b.dedupe_hash


def test_name_used_when_no_domain() -> None:
    a = leak(victim_domain=None)
    assert a.dedupe_hash == leak(victim_domain=None).dedupe_hash
    assert a.dedupe_hash != leak(victim_domain=None, victim_name="Contoso").dedupe_hash


def test_leak_without_any_victim_identity_is_unusable() -> None:
    assert not leak(victim_name=None, victim_domain=None).is_usable
    assert leak().is_usable
