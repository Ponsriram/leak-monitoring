"""Exposure detection: what it finds, what it must not, and that it never keeps the secret.

Every secret below is synthetic. Vendor-prefixed ones are assembled from pieces so this file
does not itself trip a secret scanner.
"""

from __future__ import annotations

import pytest

from intel.extract.secrets import (
    MAX_FINDINGS_PER_PAGE,
    ExposureKind,
    find_exposures,
    luhn_valid,
)

AWS_KEY = "AKIA" + "QWERTYUIOP123456"
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
STRIPE_KEY = "sk_" + "live_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
BCRYPT = "$2b$12$" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0"
VISA = "4539578763621486"
MASTERCARD = "5425233430109903"


def kinds(text: str) -> list[ExposureKind]:
    return [f.kind for f in find_exposures(text)]


# ---------------------------------------------------------------- credential pairs


def test_combo_line_is_found_and_never_stored_in_the_clear():
    [hit] = find_exposures("jane.doe@acme.com:Summer2024!")

    assert hit.kind is ExposureKind.CREDENTIAL_PAIR
    assert hit.email_domain == "acme.com"
    # The preview must not carry the password or the full local part.
    assert hit.preview == "j***@acme.com:********"
    assert "Summer2024" not in hit.preview
    assert "jane" not in hit.preview


def test_labelled_form_across_lines():
    hits = find_exposures("Username: bob\nPassword: hunter22")
    assert [h.detector for h in hits] == ["labelled_username_password"]
    assert hits[0].email_domain is None  # a username has no organisation to match


def test_labelled_email_carries_its_domain():
    [hit] = find_exposures("email: ops@acme.com  pass: Tr0ub4dor&3")
    assert hit.email_domain == "acme.com"
    assert hit.confidence == 80


@pytest.mark.parametrize(
    "text",
    [
        "contact us at sales@acme.com for details",  # an address with no password
        "https://user@host.com:8080/path",  # URL userinfo and a port
        "ssh admin@acme.com:22",  # a port
        "demo@example.com:password",  # placeholder domain and placeholder password
        "bob@acme.com:********",  # masked
        "bob@acme.com:<password>",  # template
        "Password: hunter22",  # a password with nobody attached
    ],
)
def test_prose_and_placeholders_are_not_credentials(text):
    assert find_exposures(text) == []


def test_a_combolist_outranks_a_stray_line():
    stray = find_exposures("a@acme.com:Winter-2023x")[0].confidence
    dump = "\n".join(f"user{n}@acme.com:Passw0rd{n}!" for n in range(8))
    assert all(f.confidence > stray for f in find_exposures(dump))


def test_same_credential_twice_is_one_finding():
    assert len(find_exposures("a@acme.com:Winter-2023x\na@acme.com:Winter-2023x")) == 1


def test_fingerprint_is_stable_and_depends_on_the_salt():
    [hit] = find_exposures("a@acme.com:Winter-2023x")
    assert hit.fingerprint("salt-one") == hit.fingerprint("salt-one")
    assert hit.fingerprint("salt-one") != hit.fingerprint("salt-two")
    assert "Winter" not in hit.fingerprint("salt-one")


# ---------------------------------------------------------------- hashes


def test_bcrypt_hash_is_found_and_previewed_by_scheme_only():
    [hit] = find_exposures(f"admin:{BCRYPT}")
    assert hit.kind is ExposureKind.PASSWORD_HASH
    assert hit.detector == "bcrypt"
    assert hit.preview == "$2b$12$…"


def test_a_bare_hex_digest_is_not_a_password_hash():
    # File hashes live in the IOC feeds; a bare digest is ambiguous and is left alone.
    assert find_exposures("d41d8cd98f00b204e9800998ecf8427e") == []


# ---------------------------------------------------------------- keys


def test_vendor_keys():
    text = f"aws={AWS_KEY} gh={GITHUB_TOKEN} stripe={STRIPE_KEY}"
    assert {f.detector for f in find_exposures(text)} == {
        "aws_access_key_id",
        "github_token",
        "stripe_live_key",
    }


def test_key_preview_is_masked():
    [hit] = find_exposures(AWS_KEY)
    assert hit.preview == "AKIA…3456"
    assert AWS_KEY not in hit.preview


def test_vendor_documentation_keys_are_ignored():
    assert find_exposures("AKIAIOSFODNN7EXAMPLE") == []


def test_private_key_needs_a_body():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 4
    mention = "paste your -----BEGIN PRIVATE KEY----- here"
    real = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"

    assert find_exposures(mention) == []
    [hit] = find_exposures(real)
    assert hit.kind is ExposureKind.PRIVATE_KEY
    assert body not in hit.preview


# ---------------------------------------------------------------- cards


def test_luhn():
    assert luhn_valid(VISA)
    assert not luhn_valid("4539578763621487")


def test_card_with_context_outranks_a_bare_number():
    bare = find_exposures(f"ref {VISA}")[0]
    with_context = find_exposures(f"card {VISA} exp 04/27 cvv 123")[0]

    assert bare.kind is ExposureKind.PAYMENT_CARD
    assert with_context.confidence > bare.confidence
    assert bare.preview == "4539 **** **** 1486"


def test_grouped_card_and_other_network():
    assert find_exposures("4539 5787 6362 1486 cvv 123")[0].value == VISA
    assert find_exposures(f"{MASTERCARD} cvc")[0].detector == "mastercard"


@pytest.mark.parametrize(
    "text",
    [
        "4539578763621487",  # fails the checksum
        "1111111111111111",  # one repeated digit
        "order 12345678901234567890",  # too long to be a card
        "1700000000000",  # a unix-ms timestamp
        "9539578763621486",  # no issuer range
    ],
)
def test_non_cards_are_not_cards(text):
    assert find_exposures(text) == []


# ---------------------------------------------------------------- page-level behaviour


def test_ordinary_leak_site_text_produces_nothing():
    page = (
        "Northwind Logistics northwind.com published 12 TB countdown 3 days. "
        "Contact support@leaksite.example. Login. Password reset. Tor browser required."
    )
    assert find_exposures(page) == []


def test_findings_are_capped_and_ranked_best_first():
    dump = "\n".join(f"u{n}@acme.com:Passw0rd{n}!" for n in range(MAX_FINDINGS_PER_PAGE + 50))
    found = find_exposures(f"{GITHUB_TOKEN}\n{dump}")

    assert len(found) == MAX_FINDINGS_PER_PAGE
    assert found[0].detector == "github_token"
    assert [f.confidence for f in found] == sorted((f.confidence for f in found), reverse=True)
