"""Phone numbers in scam reports: find them with a regex, confirm them, classify the report.

Three steps, each narrower than the last.

1. **Regex** (`CANDIDATE`) finds every run of digits that is *shaped* like a phone number —
   an optional `+` or `00` international prefix, an optional bracketed area code, then digit
   groups separated by spaces, dots or dashes. This follows the approach in
   https://stackoverflow.com/questions/22378736/regex-for-mobile-number-validation, widened
   from its single-country patterns to international formats because scam reports come from
   everywhere. `INDIAN_MOBILE` is that answer's Indian mobile pattern, kept as-is: a 10-digit
   number starting 6-9, with an optional +91 / 0091 / 0 prefix.

2. **Rejection** of shapes the regex cannot tell apart from a phone number but which are not
   one: IPv4 addresses, dates, times, version strings — scam posts are full of all four.

3. **Validation** with libphonenumber (the `phonenumbers` package, Google's library). A
   candidate survives only if it parses to a number that is valid for some region. This is
   what removes the long tail a regex alone lets through — scan IDs, order numbers, prices.

The report is then classified from its own words into threat types and a target audience.
Both are always non-empty: when no specific wording matches, the report is still a scam
report about a phone number, and it says so ("Phone Scam", "General Public") rather than
leaving a mandatory column blank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType

from .gazetteer import CCTLD_COUNTRY

__all__ = [
    "CANDIDATE",
    "INDIAN_MOBILE",
    "FoundNumber",
    "classify_audience",
    "classify_threats",
    "find_numbers",
    "has_phone_context",
]

# Step 1 — the candidate regex.
#
#   (?<![\w/.:#@=-])     not the middle of a word, URL, IP, hashtag, handle or query string
#   (?:\+|00)?           international prefix
#   \(?\d{1,4}\)?        first group: country, trunk or area code, optionally bracketed
#   (?:[\s.\-]*\(?\d{1,5}\)?){1,6}
#                        then one to six further groups, each optionally separated
#   (?![\w/@]|[.:]\d)    and not followed by more of a word, a path, or another dotted digit
CANDIDATE = re.compile(
    r"(?<![\w/.:#@=\-])(?:\+|00)?\(?\d{1,4}\)?(?:[\s.\-]{0,2}\(?\d{1,5}\)?){1,6}(?![\w/@]|[.:]\d)"
)

# The Stack Overflow answer's Indian mobile pattern, anchored, for a candidate with its
# separators removed.
INDIAN_MOBILE = re.compile(r"^(?:(?:\+|0{0,2})91(\s*[\-]\s*)?|[0]?)?[6789]\d{9}$")

# Step 2 — shapes that match the regex but are something else.
_NOT_A_NUMBER = (
    re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$"),  # IPv4
    re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}"),  # 2026-09-18
    re.compile(r"^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$"),  # 18/09/2026
    re.compile(r"^\d{1,2}[:.]\d{2}(?:[:.]\d{2})?$"),  # 09:40
    # 1.2.3 / 10.4.12 version strings. Short groups only: "779.222.0815" is how many US
    # posts write a phone number, and must get through.
    re.compile(r"^\d{1,2}(?:\.\d{1,3}){2,}$"),
)

# A number is only worth keeping from a post that talks about being called or messaged.
# Without this a post listing order IDs or prices produces "numbers" that happen to be
# dialable somewhere in the world.
_CONTEXT = re.compile(
    r"\b(?:call(?:s|ed|er|ing)?|phone|tel(?:ephone)?|number|num|no\.|text(?:s|ed)?|sms|"
    r"mms|whats\s?app|telegram|signal|viber|imessage|dial(?:led|ed)?|rang|ring(?:ing)?|"
    r"voicemail|voice\s?mail|mobile|cell|caller|hotline|helpline|contact|reach\s+us|"
    r"gebeld|anruf|angerufen|appel(?:é)?|llam(?:ada|ó|aron)|numéro|nummer|número|"
    r"telefon|telefono|teléfono|telefone)\b",
    re.IGNORECASE,
)

# Region guesses for numbers written without an international prefix, by post language.
# India is always tried: it is where this deployment is based, and the Stack Overflow
# pattern above is Indian.
_LANGUAGE_REGIONS: dict[str, tuple[str, ...]] = {
    "en": ("US", "GB", "IN", "CA", "AU", "NZ", "IE", "ZA", "SG"),
    "nl": ("NL", "BE"),
    "de": ("DE", "AT", "CH"),
    "fr": ("FR", "BE", "CH", "CA"),
    "es": ("ES", "MX", "AR", "CO", "CL", "PE"),
    "pt": ("BR", "PT"),
    "it": ("IT",),
    "pl": ("PL",),
    "sv": ("SE",),
    "da": ("DK",),
    "no": ("NO",),
    "fi": ("FI",),
    "ja": ("JP",),
    "hi": ("IN",),
    "ta": ("IN",),
}
_FALLBACK_REGIONS = ("IN", "US", "GB")

_LINE_TYPE = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "fixed_line",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate",
    PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.PERSONAL_NUMBER: "personal",
    PhoneNumberType.PAGER: "pager",
    PhoneNumberType.UAN: "uan",
}


@dataclass(frozen=True, slots=True)
class FoundNumber:
    e164: str
    display: str
    raw: str
    region_code: str | None
    country: str | None
    line_type: str | None


def has_phone_context(text: str) -> bool:
    return bool(_CONTEXT.search(text))


def find_numbers(text: str, *, language: str | None = None) -> list[FoundNumber]:
    """Every valid phone number in `text`, deduplicated by E.164, in order of appearance."""
    regions = _regions_for(language)
    found: dict[str, FoundNumber] = {}

    for match in CANDIDATE.finditer(text):
        raw = match.group().strip(" .-")
        # A bracket the regex took from the surrounding prose — "(775.208.9213" — rather
        # than one around an area code.
        if raw.count("(") != raw.count(")"):
            raw = raw.strip("()").strip(" .-")
        digits = re.sub(r"\D", "", raw)
        # E.164 allows at most 15 digits; nothing real is shorter than 7.
        if not 7 <= len(digits) <= 15:
            continue
        if any(pattern.match(raw) for pattern in _NOT_A_NUMBER):
            continue

        number = _parse(raw, digits, regions)
        if number is None:
            continue

        e164 = phonenumbers.format_number(number, PhoneNumberFormat.E164)
        if e164 in found:
            continue
        region = phonenumbers.region_code_for_number(number)
        found[e164] = FoundNumber(
            e164=e164,
            display=phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL),
            raw=raw,
            region_code=region,
            country=CCTLD_COUNTRY.get(region.lower(), region) if region else None,
            line_type=_LINE_TYPE.get(phonenumbers.number_type(number)),
        )

    return list(found.values())


def _regions_for(language: str | None) -> tuple[str, ...]:
    preferred = _LANGUAGE_REGIONS.get((language or "").split("-")[0].lower(), ())
    return tuple(dict.fromkeys((*preferred, *_FALLBACK_REGIONS)))


def _parse(raw: str, digits: str, regions: tuple[str, ...]) -> phonenumbers.PhoneNumber | None:
    """The first valid reading of the candidate, or None.

    Written with a `+` or `00`, the number carries its own country and needs no guess.
    Without one it is a national number, and is tried against the likely regions in turn.
    """
    international = raw.startswith("+") or raw.startswith("00")
    if international:
        attempts: tuple[tuple[str, str | None], ...] = (("+" + digits.removeprefix("00"), None),)
    elif len(digits) == 12 and digits.startswith("91") and INDIAN_MOBILE.match(digits):
        # The Stack Overflow pattern, when the number carries India's 91 country code
        # without a `+`. Nothing shorter is decided on shape alone: a bare ten digits
        # starting 6-9 ("779.222.0815") is as likely a US number, and a leading 0 plus ten
        # digits ("07371 303 644") is exactly how UK mobiles are written. Those go to the
        # region guesses below, which try India among the others.
        attempts = (("+" + digits, None),)
    else:
        attempts = tuple((digits, region) for region in regions)

    for candidate, region in attempts:
        try:
            number = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(number):
            return number
    return None


# --- classification --------------------------------------------------------------------------

# (label, pattern). A report gets every label whose pattern matches its text. Order is the
# order the chips render in: the delivery channel first, then what the scammer pretended to
# be, then what they were after.
_THREATS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("Smishing", r"\b(?:sms|smishing|text(?:ed|s)?\s+(?:me|message|from)|texted)\b"),
        (
            "Vishing",
            r"\b(?:vishing|scam\s*call|robocall|cold\s*call|called\s+me|"
            r"(?:got|had|received)\s+a\s+call|rang\s+me|voicemail|gebeld|angerufen)\b",
        ),
        ("WhatsApp Scam", r"\bwhats\s?app\b"),
        ("Telegram Scam", r"\btelegram\b"),
        (
            "Tech Support Scam",
            r"\b(?:tech(?:nical)?\s+support|microsoft|windows\s+support|"
            r"apple\s+support|anydesk|teamviewer|remote\s+access|"
            r"virus\s+on\s+your|your\s+computer)\b",
        ),
        (
            "Bank Impersonation",
            r"\b(?:bank|fraud\s+(?:department|team)|card\s+(?:has\s+been\s+)?"
            r"(?:blocked|suspended)|account\s+(?:locked|suspended)|"
            r"unauthori[sz]ed\s+transaction)\b",
        ),
        (
            "Delivery Scam",
            r"\b(?:parcel|package|delivery|courier|usps|ups|fedex|dhl|"
            r"royal\s+mail|evri|postnl|india\s+post|customs\s+fee)\b",
        ),
        (
            "Government Impersonation",
            r"\b(?:irs|hmrc|dwp|social\s+security|ssa|police|"
            r"immigration|court|arrest\s+warrant|tax\s+refund|"
            r"income\s+tax|customs|trai|cbi|narcotics)\b",
        ),
        ("Toll Scam", r"\b(?:toll|e-?zpass|sunpass|fastag|unpaid\s+toll)\b"),
        (
            "Telecom Impersonation",
            r"\b(?:t-mobile|verizon|at&t|vodafone|airtel|jio|bsnl|"
            r"movistar|o2|ee\s+customer|bt|bell(?:\s+canada)?|three\s+network|"
            r"virgin\s+media|rogers|telus|openreach|sim\s+(?:card\s+)?(?:block|"
            r"swap|deactivat))\w*",
        ),
        (
            "Investment Scam",
            r"\b(?:invest(?:ment)?|crypto|bitcoin|trading|forex|returns|"
            r"profit)\b",
        ),
        (
            "Job Scam",
            r"\b(?:job\s+offer|recruit(?:er|ment)|work\s+from\s+home|"
            r"part[\s-]time|hiring|task\s+scam)\b",
        ),
        ("Romance Scam", r"\b(?:romance|dating|pig\s+butchering|wrong\s+number)\b"),
        (
            "Prize Scam",
            r"\b(?:lottery|prize|you(?:'ve|\s+have)\s+won|winner|gift\s+card|"
            r"reward\s+points?)\b",
        ),
        (
            "Brand Impersonation",
            r"\b(?:amazon|paypal|netflix|apple|google|facebook|meta|"
            r"instagram|ebay|flipkart|paytm)\b",
        ),
        ("Extortion", r"\b(?:sextortion|blackmail|extort\w*|digital\s+arrest)\b"),
        (
            "Credential Harvesting",
            r"\b(?:otp|one[\s-]time\s+(?:pass(?:word|code)|code)|"
            r"verification\s+code|password|login|log\s+in|"
            r"pin\s+(?:number|code))\b",
        ),
        (
            "Payment Fraud",
            r"\b(?:upi|gift\s+cards?|wire\s+transfer|zelle|venmo|cash\s+app|"
            r"western\s+union|refund)\b",
        ),
    )
)

_AUDIENCE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("Bank Customers", r"\b(?:bank|credit\s+card|debit\s+card|account\s+holder)\w*"),
        (
            "Parcel Recipients",
            r"\b(?:parcel|package|delivery|courier|usps|ups|fedex|dhl|"
            r"royal\s+mail|evri|postnl|india\s+post)\b",
        ),
        ("Taxpayers", r"\b(?:irs|hmrc|tax\s+refund|income\s+tax|tax\s+return)\b"),
        (
            "Windows / Computer Users",
            r"\b(?:microsoft|windows|computer|pc|laptop|anydesk|"
            r"teamviewer)\b",
        ),
        ("Apple Users", r"\b(?:apple|icloud|iphone)\b"),
        ("Online Shoppers", r"\b(?:amazon|ebay|flipkart|order\s+(?:number|confirmation))\b"),
        ("PayPal Users", r"\bpaypal\b"),
        (
            "Mobile Subscribers",
            r"\b(?:t-mobile|verizon|at&t|vodafone|airtel|jio|bsnl|"
            r"movistar|bt|bell(?:\s+canada)?|three\s+network|virgin\s+media|rogers|"
            r"telus|sim\s+card|carrier)\b",
        ),
        (
            "Drivers",
            r"\b(?:toll|e-?zpass|sunpass|fastag|dmv|parking|traffic\s+(?:fine|"
            r"ticket))\b",
        ),
        ("Job Seekers", r"\b(?:job|recruit\w*|hiring|work\s+from\s+home|part[\s-]time)\b"),
        ("Investors / Crypto Holders", r"\b(?:invest\w*|crypto|bitcoin|trading|forex)\b"),
        (
            "Social Media Users",
            r"\b(?:facebook|instagram|whatsapp|telegram|tiktok|"
            r"snapchat)\b",
        ),
        (
            "Social Security / Benefit Recipients",
            r"\b(?:social\s+security|dwp|medicare|"
            r"pension|benefits?)\b",
        ),
        (
            "Elderly People",
            r"\b(?:elderly|grand(?:ma|pa|mother|father|parents?)|"
            r"senior\s+citizens?|pensioners?)\b",
        ),
        ("Businesses", r"\b(?:invoice|supplier|vendor|ceo|cfo|payroll)\b"),
        ("Online Daters", r"\b(?:dating|romance|tinder|bumble|hinge)\b"),
    )
)


def classify_threats(text: str) -> list[str]:
    """Every threat type the report's words support. Never empty."""
    found = [label for label, pattern in _THREATS if pattern.search(text)]
    return found or ["Phone Scam"]


def classify_audience(text: str) -> list[str]:
    """Who the scam targets, from the report's words. Never empty.

    The number's own country is deliberately not used: scammers call across borders on
    foreign and VoIP numbers, so "a +44 number" says where the line is registered, not who
    was being targeted. The Country column carries it as what it is.
    """
    found = [label for label, pattern in _AUDIENCE if pattern.search(text)]
    return found or ["General Public"]
