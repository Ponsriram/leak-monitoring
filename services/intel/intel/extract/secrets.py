"""Find credentials, secrets and payment cards in page text — without keeping them.

A leak-site listing is usually a victim name and a countdown, but the pages worth reading
closest are the ones that *are* the leak: a combolist pasted into a post, a build log with an
AWS key in it, a dump of password hashes. This module finds those, and is deliberately built
so that finding one does not turn our database into a second copy of the leak.

**Nothing found here is stored in the clear.** An `Exposure` carries the real value only in
memory, for as long as it takes to compute two derived fields:

* `preview` — what an analyst sees: `j***@example.com:********`, `AKIA…QRST`. Enough to tell
  one finding from another and to recognise it, not enough to use it.
* `fingerprint(salt)` — HMAC-SHA256 of the value under a deployment secret. It is how the same
  finding on a re-crawled page is recognised as the same row, and it is not reversible by a
  dictionary attack without the salt, which a plain SHA-256 of an email:password line would be.

The email's *domain* is kept in the clear on purpose. It is an organisation, not a person, and
it is what the watchlist matches on ("anything exposed for `acme.com`").

**Precision over recall.** Every rule is anchored to something structural — a vendor prefix, a
checksum, a labelled field, a hash format — and the generic ones (combo lines, card numbers)
carry a lower confidence and are corroborated before they are trusted. A page that merely
contains the word "password" produces nothing, and neither does a bare 32-character hex string:
that is a file hash as often as a credential, and the IOC feeds already own that namespace.

No ML, no network, no I/O: a pure function of the text, like the rest of `extract/`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Exposure", "ExposureKind", "find_exposures", "luhn_valid"]

# A page that is one enormous combolist would otherwise write tens of thousands of rows from a
# single fetch. Beyond this the page is already established as a dump, and the rest is volume.
MAX_FINDINGS_PER_PAGE = 2000

# Combo lines are common in prose ("write to a@b.com:"), so one hit proves little. Several in
# one page is a dump. The bonus applies to the whole page, which is why confidence is settled
# after every finding is collected rather than as each is found.
_COMBOLIST_THRESHOLD = 5
_COMBOLIST_BONUS = 15


class ExposureKind(StrEnum):
    """Mirrors the `exposure_kind` Postgres enum."""

    CREDENTIAL_PAIR = "credential_pair"
    PASSWORD_HASH = "password_hash"
    API_KEY = "api_key"
    PAYMENT_CARD = "payment_card"
    PRIVATE_KEY = "private_key"


@dataclass(slots=True)
class Exposure:
    kind: ExposureKind
    #: Which rule matched — "aws_access_key_id", "email_password", "bcrypt". Shown in the UI.
    detector: str
    #: The real value. Held in memory only; `storage.upsert_exposures` never receives it.
    value: str
    preview: str
    confidence: int
    email_domain: str | None = None

    def fingerprint(self, salt: str) -> str:
        """Keyed identity of this finding, stable across re-crawls of the same page."""
        message = f"{self.kind.value}\0{self.value}".encode()
        return hmac.new(salt.encode(), message, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- masking


def _mask_email(local: str, domain: str) -> str:
    return f"{local[:1]}***@{domain}"


def _mask_token(value: str) -> str:
    """`AKIAXXXXXXXXXXXXQRST` -> `AKIA…QRST`. Short values are fully hidden."""
    if len(value) < 12:
        return value[:2] + "…"
    return f"{value[:4]}…{value[-4:]}"


def _mask_card(digits: str) -> str:
    return f"{digits[:4]} **** **** {digits[-4:]}"


# --------------------------------------------------------------------------- credential pairs

# A domain that exists in documentation and tutorials, never in a real dump. An address here is
# a placeholder, and placeholders are the main source of false positives in this rule.
_PLACEHOLDER_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "domain.com",
        "yourdomain.com",
        "mydomain.com",
        "email.example",
        "localhost",
    }
)

# Values that are a mask or a template rather than a password. A password that is literally
# "password" is excluded too: in a real dump it is vanishingly rare next to its use as an
# example in documentation, and keeping it would trade a handful of true hits for a flood.
_PLACEHOLDER_PASSWORD = re.compile(
    r"^(?:[*x•.#_-]{3,}|<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\$\{.*\}|%s|"
    r"(?:your_?)?pass(?:word)?|pwd|redacted|hidden|masked|null|none|n/?a|secret|changeme)$",
    re.IGNORECASE,
)

_EMAIL = (
    r"(?P<local>[A-Za-z0-9._%+-]{1,64})@"
    r"(?P<domain>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
)

# email:password, email;password, email|password — the combolist line.
#
# `(?<![/\w.%+-])` keeps it from starting in the middle of a URL's userinfo, and the password
# group refuses a leading `/` and a pure port number: `https://u@host.com:8080/x` is a URL, and
# without those two guards its port and path would be read as a password.
_COMBO = re.compile(
    rf"(?<![/\w.%+-]){_EMAIL}[:;|](?P<pw>(?![/])\S{{4,64}})",
)

# "Username: bob  Password: hunter2", "email=a@b.com pass=x", across one or two lines.
_LABELLED = re.compile(
    r"\b(?:user(?:name)?|login|e-?mail)\s*[:=]\s*(?P<id>[^\s,;|]{2,80})"
    r"[\s,;|]+(?:pass(?:word)?|pwd|pw)\s*[:=]\s*(?P<pw>\S{3,64})",
    re.IGNORECASE,
)

_EMAIL_ONLY = re.compile(rf"^{_EMAIL}$")


def _acceptable_password(password: str) -> bool:
    if _PLACEHOLDER_PASSWORD.match(password):
        return False
    # A port number is the commonest thing a `host:NNNN` shape turns out to be.
    return not (password.isdigit() and len(password) <= 5)


def _credential_pairs(text: str) -> list[Exposure]:
    found: list[Exposure] = []

    for match in _COMBO.finditer(text):
        domain = match.group("domain").lower()
        password = match.group("pw").rstrip(".,;)")
        if domain in _PLACEHOLDER_DOMAINS or not _acceptable_password(password):
            continue
        local = match.group("local")
        found.append(
            Exposure(
                kind=ExposureKind.CREDENTIAL_PAIR,
                detector="email_password",
                value=f"{local.lower()}@{domain}:{password}",
                preview=f"{_mask_email(local, domain)}:********",
                confidence=65,
                email_domain=domain,
            )
        )

    for match in _LABELLED.finditer(text):
        identity = match.group("id").strip("<>\"'")
        password = match.group("pw").strip("\"'").rstrip(".,;)")
        if not _acceptable_password(password):
            continue

        email = _EMAIL_ONLY.match(identity)
        if email:
            domain = email.group("domain").lower()
            if domain in _PLACEHOLDER_DOMAINS:
                continue
            found.append(
                Exposure(
                    kind=ExposureKind.CREDENTIAL_PAIR,
                    detector="labelled_email_password",
                    value=f"{email.group('local').lower()}@{domain}:{password}",
                    preview=f"{_mask_email(email.group('local'), domain)}:********",
                    confidence=80,
                    email_domain=domain,
                )
            )
        else:
            # A bare username has no organisation to attach it to, so it cannot be matched
            # against a watchlist by domain — but it is still a credential in a dump.
            found.append(
                Exposure(
                    kind=ExposureKind.CREDENTIAL_PAIR,
                    detector="labelled_username_password",
                    value=f"{identity.lower()}:{password}",
                    preview=f"{identity[:1]}***:********",
                    confidence=70,
                )
            )

    return found


# --------------------------------------------------------------------------- password hashes

# Only formats with a self-describing prefix. A bare 32/40/64-hex string is deliberately not
# here: it is as likely a file hash, and the IOC feeds already carry those.
_HASHES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bcrypt", re.compile(r"\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}")),
    ("sha512crypt", re.compile(r"\$6\$(?:rounds=\d+\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}")),
    ("sha256crypt", re.compile(r"\$5\$(?:rounds=\d+\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{43}")),
    ("md5crypt", re.compile(r"\$1\$[./A-Za-z0-9]{0,8}\$[./A-Za-z0-9]{22}")),
    (
        "argon2",
        re.compile(
            r"\$argon2(?:id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+"
        ),
    ),
)


def _password_hashes(text: str) -> list[Exposure]:
    found: list[Exposure] = []
    for name, pattern in _HASHES:
        for match in pattern.finditer(text):
            value = match.group(0)
            found.append(
                Exposure(
                    kind=ExposureKind.PASSWORD_HASH,
                    detector=name,
                    value=value,
                    # The scheme prefix is the useful part and reveals nothing about the hash.
                    preview=f"{value[:7]}…",
                    confidence=85,
                )
            )
    return found


# --------------------------------------------------------------------------- API keys & secrets

# Each rule is a vendor-assigned prefix plus a fixed body, which is what makes these reliable:
# a random string does not begin `ghp_` by accident.
_KEY_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 80),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), 90),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), 85),
    ("stripe_live_key", re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{20,}\b"), 90),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 80),
)

# The keys vendors publish in their own documentation. Matching them reports the docs.
_KNOWN_EXAMPLE_KEYS = frozenset({"AKIAIOSFODNN7EXAMPLE", "AKIAI44QH8DHBEXAMPLE"})

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<type>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?)-----"
    r"(?P<body>[\s\S]{0,4096}?)(?:-----END|$)"
)


def _api_keys(text: str) -> list[Exposure]:
    found: list[Exposure] = []
    for name, pattern, confidence in _KEY_RULES:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in _KNOWN_EXAMPLE_KEYS or value.endswith("EXAMPLE"):
                continue
            found.append(
                Exposure(
                    kind=ExposureKind.API_KEY,
                    detector=name,
                    value=value,
                    preview=_mask_token(value),
                    confidence=confidence,
                )
            )

    for match in _PRIVATE_KEY.finditer(text):
        body = re.sub(r"\s+", "", match.group("body"))
        # A header with no body is a mention ("paste your -----BEGIN PRIVATE KEY----- here"),
        # not a key. Real key material is base64 and runs to hundreds of characters.
        if len(body) < 64:
            continue
        found.append(
            Exposure(
                kind=ExposureKind.PRIVATE_KEY,
                detector=match.group("type").strip().lower().replace(" ", "_"),
                value=body,
                preview=f"-----BEGIN {match.group('type')}-----",
                confidence=95,
            )
        )
    return found


# --------------------------------------------------------------------------- payment cards

# 13-19 digits, optionally grouped by single spaces or dashes. The lookarounds refuse to start
# or end inside a longer digit run, which is what keeps order numbers and timestamps out.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")

_CARD_CONTEXT = re.compile(r"\b(?:cvv2?|cvc|cid|exp(?:iry|ires?|iration)?|card|visa|master)", re.I)
_CARD_CONTEXT_WINDOW = 60


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum every real card number satisfies."""
    if not digits.isdigit() or len(digits) < 12:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_network(digits: str) -> str | None:
    """The network whose issuer range and length this number fits, else None."""
    n = len(digits)
    if digits[0] == "4" and n in (13, 16, 19):
        return "visa"
    prefix2, prefix4 = int(digits[:2]), int(digits[:4])
    if (51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720) and n == 16:
        return "mastercard"
    if prefix2 in (34, 37) and n == 15:
        return "amex"
    discover = digits.startswith(("6011", "65")) or 644 <= int(digits[:3]) <= 649
    if discover and n in (16, 19):
        return "discover"
    return None


def _payment_cards(text: str) -> list[Exposure]:
    found: list[Exposure] = []
    for match in _CARD_CANDIDATE.finditer(text):
        raw = match.group(0)
        digits = re.sub(r"[ -]", "", raw)

        # 4111111111111111 passes every check and is in every tutorial. A run of one repeated
        # digit passes Luhn about one time in ten and is never a card.
        if len(set(digits)) < 4:
            continue
        if _card_network(digits) is None or not luhn_valid(digits):
            continue

        window = text[
            max(0, match.start() - _CARD_CONTEXT_WINDOW) : match.end() + _CARD_CONTEXT_WINDOW
        ]
        # A bare 16-digit number that checksums is only about one in ten likely to be a card
        # by chance — enough that it needs corroboration to rank high. A card keyword nearby
        # is that corroboration.
        confidence = 80 if _CARD_CONTEXT.search(window) else 50
        found.append(
            Exposure(
                kind=ExposureKind.PAYMENT_CARD,
                detector=_card_network(digits) or "card",
                value=digits,
                preview=_mask_card(digits),
                confidence=confidence,
            )
        )
    return found


# --------------------------------------------------------------------------- entry point


def find_exposures(text: str) -> list[Exposure]:
    """Every credential, secret and card number in `text`, deduplicated and scored.

    The same value found by two rules, or twice on one page, is one finding at the highest
    confidence any rule gave it.
    """
    findings = [
        *_credential_pairs(text),
        *_password_hashes(text),
        *_api_keys(text),
        *_payment_cards(text),
    ]

    # Settle the page-level corroboration before deduplicating, so the bonus counts hits.
    pairs = sum(1 for f in findings if f.kind is ExposureKind.CREDENTIAL_PAIR)
    if pairs >= _COMBOLIST_THRESHOLD:
        for finding in findings:
            if finding.kind is ExposureKind.CREDENTIAL_PAIR:
                finding.confidence = min(95, finding.confidence + _COMBOLIST_BONUS)

    best: dict[tuple[ExposureKind, str], Exposure] = {}
    for finding in findings:
        key = (finding.kind, finding.value)
        if key not in best or finding.confidence > best[key].confidence:
            best[key] = finding

    ranked = sorted(best.values(), key=lambda f: f.confidence, reverse=True)
    return ranked[:MAX_FINDINGS_PER_PAGE]
