"""Turning what a person typed into a domain we can look up.

Every enricher in this package takes a normalized apex domain, and getting that right is the
difference between finding a company and finding nothing: `WWW.Framegroup.com.au/`,
`framegroup.com.au` and `https://framegroup.com.au/about` are one target, and an enricher
that treats them as three re-fetches the same RDAP record three times and writes three cache
rows that can disagree with each other.
"""

from __future__ import annotations

import re

__all__ = ["apex_domain", "looks_like_domain", "normalize_domain", "normalize_query"]

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HOST_CHARS = re.compile(r"^[a-z0-9.-]+$")
_WHITESPACE = re.compile(r"\s+")

"""Public suffixes that are two labels deep.

The apex of `framegroup.com.au` is the whole of `framegroup.com.au`, but the apex of
`example.com` is `example.com` — "last two labels" is right for one and wrong for the other.
Getting it wrong matters: `com.au` is not a company, and looking it up returns the registry's
own record rather than the victim's.

The complete answer is the Public Suffix List, which is ~9000 entries that change monthly and
would mean either a new dependency that phones home for updates or a vendored file that goes
stale silently. This covers the suffixes that actually appear in leak-site victim domains.
A miss degrades gracefully — `something.co.zz` yields `co.zz`, the lookups return nothing,
and the hunt reports no registration data rather than wrong registration data.
"""
_TWO_LABEL_SUFFIXES = frozenset(
    {
        # United Kingdom
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "net.uk",
        "ltd.uk",
        "plc.uk",
        # Australia / New Zealand
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "asn.au",
        "id.au",
        "co.nz",
        "net.nz",
        "org.nz",
        "govt.nz",
        "ac.nz",
        # India
        "co.in",
        "net.in",
        "org.in",
        "gen.in",
        "firm.in",
        "ind.in",
        "ac.in",
        "gov.in",
        # Brazil / Latin America
        "com.br",
        "net.br",
        "org.br",
        "gov.br",
        "edu.br",
        "com.ar",
        "com.mx",
        "com.co",
        "com.pe",
        "com.uy",
        "com.ve",
        "com.ec",
        # Asia-Pacific
        "co.jp",
        "or.jp",
        "ne.jp",
        "ac.jp",
        "go.jp",
        "co.kr",
        "or.kr",
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "com.hk",
        "com.tw",
        "com.sg",
        "com.my",
        "com.ph",
        "co.id",
        "co.th",
        "com.vn",
        # Europe
        "co.at",
        "or.at",
        "com.es",
        "com.pl",
        "com.tr",
        "com.ua",
        "com.ru",
        "com.gr",
        "com.pt",
        "com.cy",
        "com.hr",
        "com.mt",
        "co.rs",
        # Middle East / Africa
        "co.il",
        "com.sa",
        "com.eg",
        "com.ng",
        "co.za",
        "org.za",
        "com.tn",
        "com.ma",
        # North America
        "com.ca",
    }
)


def normalize_query(raw: str) -> str:
    """Lowercase and collapse whitespace. The cache key for a hunt.

    `"  Frame   Group "` and `"frame group"` are the same search and must not run the same
    six network lookups twice.
    """
    return _WHITESPACE.sub(" ", raw.strip()).lower()


def normalize_domain(raw: str | None) -> str | None:
    """Strip a URL down to a bare, lowercase hostname. None if there isn't one in there.

    Handles the shapes that actually arrive: a pasted URL, a hostname with `www.`, a domain
    with a trailing dot, and a hostname carrying a port.
    """
    if not raw:
        return None

    value = raw.strip().lower()
    if not value:
        return None

    value = _SCHEME.sub("", value)
    # Anything after the authority is path, query or fragment — none of it is the host.
    for separator in ("/", "?", "#"):
        value = value.split(separator, 1)[0]
    # userinfo@host, and host:port.
    value = value.rsplit("@", 1)[-1]
    value = value.split(":", 1)[0]
    value = value.rstrip(".")

    if value.startswith("www."):
        value = value[4:]

    if not value or "." not in value or not _HOST_CHARS.match(value):
        return None
    # A label may not start or end with a hyphen, and empty labels mean a doubled dot.
    labels = value.split(".")
    if any(not label or label.startswith("-") or label.endswith("-") for label in labels):
        return None

    return value


def apex_domain(raw: str | None) -> str | None:
    """The registrable domain — what RDAP and certificate transparency want.

    `mail.corp.framegroup.com.au` -> `framegroup.com.au`, `shop.example.com` -> `example.com`.
    """
    host = normalize_domain(raw)
    if host is None:
        return None

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def looks_like_domain(raw: str) -> bool:
    """Whether a search term is a domain rather than a company name.

    Decides which half of a hunt runs first: a domain goes straight to the registration and
    liveness lookups, a name has to be matched against what we already hold before there is
    anything to look up.
    """
    candidate = normalize_domain(raw)
    if candidate is None:
        return False
    # A company name with a space in it never survives normalization as a valid host, but
    # "Contoso.Manufacturing" would — require the last label to look like a TLD.
    tld = candidate.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()
