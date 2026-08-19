"""Lookup tables for the two tags a listing almost never labels: where and what.

`victim_country` and `victim_sector` have been columns on `leaks` since the first migration
and nothing has ever written to either. They are also the two fields an analyst reaches for
first — "is anything in our region being hit, and is it our industry?" — so leaving them
null makes the whole table answer only "who", never "where".

Neither field is stated in a parseable form on a leak site. Some listings print a country
outright; most print a company name, a domain and a size. So there are two routes here and
the pipeline uses both:

  * a gazetteer of country names and their aliases, for the listings that say it;
  * the domain's ccTLD, for the ones that don't — which is most of them, and which is free.

The ccTLD route needs the exclusions below to be worth anything. `.io`, `.co`, `.ai` and
friends are country codes that are sold worldwide as generic domains; reading them as
locations would file a Silicon Valley startup under the British Indian Ocean Territory.
"""

from __future__ import annotations

import re

# Canonical country name -> the strings a page might use for it. The canonical form is what
# lands in the database, so the dashboard's country filter shows one entry per country
# rather than one per spelling.
#
# Aliases are lowercase and matched whole-word. Names that are ordinary English words or
# common place names elsewhere — Chad, Niger, Jersey, Georgia — are deliberately absent: a
# wrong country in the column is worse than a missing one, because once written the two are
# indistinguishable.
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "United States": (
        "united states", "united states of america", "usa", "u.s.a.", "u.s.",
        "america", "american",
    ),
    "United Kingdom": (
        "united kingdom", "uk", "u.k.", "great britain", "britain", "british",
        "england", "scotland", "wales", "northern ireland",
    ),
    "Canada": ("canada", "canadian"),
    "Australia": ("australia", "australian"),
    "New Zealand": ("new zealand",),
    "Ireland": ("ireland", "irish", "republic of ireland"),
    "Germany": ("germany", "german", "deutschland"),
    "France": ("france", "french"),
    "Italy": ("italy", "italian", "italia"),
    "Spain": ("spain", "spanish", "españa", "espana"),
    "Portugal": ("portugal", "portuguese"),
    "Netherlands": ("netherlands", "the netherlands", "holland", "dutch"),
    "Belgium": ("belgium", "belgian"),
    "Luxembourg": ("luxembourg",),
    "Switzerland": ("switzerland", "swiss"),
    "Austria": ("austria", "austrian"),
    "Denmark": ("denmark", "danish"),
    "Sweden": ("sweden", "swedish"),
    "Norway": ("norway", "norwegian"),
    "Finland": ("finland", "finnish"),
    "Iceland": ("iceland", "icelandic"),
    "Poland": ("poland", "polish", "polska"),
    "Czechia": ("czechia", "czech republic", "czech"),
    "Slovakia": ("slovakia", "slovak"),
    "Hungary": ("hungary", "hungarian"),
    "Romania": ("romania", "romanian"),
    "Bulgaria": ("bulgaria", "bulgarian"),
    "Greece": ("greece", "greek"),
    "Croatia": ("croatia", "croatian"),
    "Slovenia": ("slovenia", "slovenian"),
    "Serbia": ("serbia", "serbian"),
    "Bosnia and Herzegovina": ("bosnia", "bosnia and herzegovina", "herzegovina"),
    "Albania": ("albania", "albanian"),
    "North Macedonia": ("north macedonia", "macedonia"),
    "Montenegro": ("montenegro",),
    "Estonia": ("estonia", "estonian"),
    "Latvia": ("latvia", "latvian"),
    "Lithuania": ("lithuania", "lithuanian"),
    "Ukraine": ("ukraine", "ukrainian"),
    "Belarus": ("belarus", "belarusian"),
    "Russia": ("russia", "russian", "russian federation"),
    "Moldova": ("moldova", "moldovan"),
    "Armenia": ("armenia", "armenian"),
    "Azerbaijan": ("azerbaijan", "azerbaijani"),
    "Kazakhstan": ("kazakhstan",),
    "Uzbekistan": ("uzbekistan",),
    "Turkey": ("türkiye", "turkiye", "republic of turkey", "turkish"),
    "Israel": ("israel", "israeli"),
    "Saudi Arabia": ("saudi arabia", "saudi", "ksa"),
    "United Arab Emirates": ("united arab emirates", "uae", "u.a.e.", "emirates", "dubai"),
    "Qatar": ("qatar", "qatari"),
    "Kuwait": ("kuwait", "kuwaiti"),
    "Bahrain": ("bahrain",),
    "Oman": ("oman", "omani"),
    "Jordan": ("jordan", "jordanian"),
    "Lebanon": ("lebanon", "lebanese"),
    "Egypt": ("egypt", "egyptian"),
    "Morocco": ("morocco", "moroccan"),
    "Algeria": ("algeria", "algerian"),
    "Tunisia": ("tunisia", "tunisian"),
    "Libya": ("libya", "libyan"),
    "Nigeria": ("nigeria", "nigerian"),
    "Ghana": ("ghana", "ghanaian"),
    "Kenya": ("kenya", "kenyan"),
    "Tanzania": ("tanzania", "tanzanian"),
    "Uganda": ("uganda", "ugandan"),
    "Ethiopia": ("ethiopia", "ethiopian"),
    "South Africa": ("south africa", "south african"),
    "Zimbabwe": ("zimbabwe",),
    "Zambia": ("zambia",),
    "Botswana": ("botswana",),
    "Namibia": ("namibia",),
    "Mozambique": ("mozambique",),
    "Angola": ("angola",),
    "Senegal": ("senegal",),
    "Ivory Coast": ("ivory coast", "cote d'ivoire", "côte d'ivoire"),
    "Cameroon": ("cameroon",),
    "India": ("india", "indian", "bharat"),
    "Pakistan": ("pakistan", "pakistani"),
    "Bangladesh": ("bangladesh", "bangladeshi"),
    "Sri Lanka": ("sri lanka", "sri lankan"),
    "Nepal": ("nepal", "nepalese"),
    "China": ("china", "chinese", "prc", "people's republic of china"),
    "Hong Kong": ("hong kong", "hongkong"),
    "Taiwan": ("taiwan", "taiwanese"),
    "Japan": ("japan", "japanese"),
    "South Korea": ("south korea", "korea", "republic of korea", "korean"),
    "Singapore": ("singapore", "singaporean"),
    "Malaysia": ("malaysia", "malaysian"),
    "Indonesia": ("indonesia", "indonesian"),
    "Thailand": ("thailand", "thai"),
    "Vietnam": ("vietnam", "viet nam", "vietnamese"),
    "Philippines": ("philippines", "the philippines", "filipino"),
    "Cambodia": ("cambodia", "cambodian"),
    "Myanmar": ("myanmar", "burma"),
    "Mexico": ("mexico", "mexican", "méxico"),
    "Guatemala": ("guatemala",),
    "Costa Rica": ("costa rica", "costa rican"),
    "Panama": ("panama", "panamanian"),
    "Dominican Republic": ("dominican republic",),
    "Puerto Rico": ("puerto rico", "puerto rican"),
    "Brazil": ("brazil", "brazilian", "brasil"),
    "Argentina": ("argentina", "argentinian", "argentine"),
    "Chile": ("chile", "chilean"),
    "Colombia": ("colombia", "colombian"),
    "Peru": ("peru", "peruvian"),
    "Ecuador": ("ecuador", "ecuadorian"),
    "Uruguay": ("uruguay", "uruguayan"),
    "Paraguay": ("paraguay",),
    "Bolivia": ("bolivia", "bolivian"),
    "Venezuela": ("venezuela", "venezuelan"),
    "Cyprus": ("cyprus", "cypriot"),
    "Malta": ("malta", "maltese"),
}

# ccTLD -> country, for the far more common listing that names no country but does show a
# domain. Only codes whose registry is genuinely national are here.
CCTLD_COUNTRY: dict[str, str] = {
    "us": "United States", "uk": "United Kingdom", "gb": "United Kingdom",
    "ca": "Canada", "au": "Australia", "nz": "New Zealand", "ie": "Ireland",
    "de": "Germany", "fr": "France", "it": "Italy", "es": "Spain", "pt": "Portugal",
    "nl": "Netherlands", "be": "Belgium", "lu": "Luxembourg", "ch": "Switzerland",
    "at": "Austria", "dk": "Denmark", "se": "Sweden", "no": "Norway", "fi": "Finland",
    "is": "Iceland", "pl": "Poland", "cz": "Czechia", "sk": "Slovakia", "hu": "Hungary",
    "ro": "Romania", "bg": "Bulgaria", "gr": "Greece", "hr": "Croatia", "si": "Slovenia",
    "rs": "Serbia", "ba": "Bosnia and Herzegovina", "al": "Albania",
    "mk": "North Macedonia", "ee": "Estonia", "lv": "Latvia", "lt": "Lithuania",
    "ua": "Ukraine", "by": "Belarus", "ru": "Russia", "md": "Moldova",
    "az": "Azerbaijan", "kz": "Kazakhstan", "uz": "Uzbekistan",
    "tr": "Turkey", "il": "Israel", "sa": "Saudi Arabia", "ae": "United Arab Emirates",
    "qa": "Qatar", "kw": "Kuwait", "bh": "Bahrain", "om": "Oman", "jo": "Jordan",
    "lb": "Lebanon", "eg": "Egypt", "ma": "Morocco", "dz": "Algeria", "tn": "Tunisia",
    "ng": "Nigeria", "gh": "Ghana", "ke": "Kenya", "tz": "Tanzania", "ug": "Uganda",
    "et": "Ethiopia", "za": "South Africa", "zw": "Zimbabwe", "zm": "Zambia",
    "bw": "Botswana", "na": "Namibia", "mz": "Mozambique", "ao": "Angola",
    "sn": "Senegal", "ci": "Ivory Coast", "cm": "Cameroon",
    "in": "India", "pk": "Pakistan", "bd": "Bangladesh", "lk": "Sri Lanka",
    "np": "Nepal", "cn": "China", "hk": "Hong Kong", "tw": "Taiwan", "jp": "Japan",
    "kr": "South Korea", "sg": "Singapore", "my": "Malaysia", "id": "Indonesia",
    "th": "Thailand", "vn": "Vietnam", "ph": "Philippines", "kh": "Cambodia",
    "mm": "Myanmar", "mx": "Mexico", "gt": "Guatemala", "cr": "Costa Rica",
    "pa": "Panama", "do": "Dominican Republic", "pr": "Puerto Rico", "br": "Brazil",
    "ar": "Argentina", "cl": "Chile", "pe": "Peru", "ec": "Ecuador", "uy": "Uruguay",
    "py": "Paraguay", "bo": "Bolivia", "ve": "Venezuela", "cy": "Cyprus", "mt": "Malta",
}

# Country codes sold as generic domains the world over. A `.io` company is almost never in
# the British Indian Ocean Territory and a `.co` one is rarely Colombian, so inferring a
# location from these is worse than inferring nothing: a wrong country in the column is
# indistinguishable from a right one.
GENERIC_CCTLDS = frozenset(
    {
        "io", "co", "ai", "me", "tv", "cc", "ly", "sh", "gg", "im", "to", "fm", "am",
        "st", "ws", "la", "gl", "tk", "ml", "ga", "cf", "nu", "vc", "ag", "as", "bz",
    }
)

def _build_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in COUNTRY_ALIASES.items():
        lookup[canonical.lower()] = canonical
        for alias in aliases:
            lookup[alias] = canonical
    return lookup


_COUNTRY_LOOKUP = _build_lookup()

def _alternation(terms: list[str]) -> str:
    r"""One `\b(a|b|c)\b` over every term, longest first.

    Longest-first is load-bearing: Python's alternation is first-match, so with the shorter
    term earlier "united states of america" would match as "united states" and leave "of
    america" dangling.

    Built once at import. Matching a page against several hundred alternatives in one
    compiled pattern is a single pass; a loop over several hundred patterns is not.
    """
    ordered = sorted(terms, key=len, reverse=True)
    return r"\b(" + "|".join(re.escape(term) for term in ordered) + r")\b"


COUNTRY_PATTERN = re.compile(_alternation(list(_COUNTRY_LOOKUP)), re.IGNORECASE)


def parse_country(raw: str | None) -> str | None:
    """Normalize a country mention to its canonical name. None when it names no country."""
    if not raw:
        return None

    text = raw.strip().lower()
    if not text:
        return None

    direct = _COUNTRY_LOOKUP.get(text)
    if direct is not None:
        return direct

    # The span may carry decoration ("Country: Germany", "based in Germany").
    match = COUNTRY_PATTERN.search(text)
    return _COUNTRY_LOOKUP.get(match.group(1).lower()) if match else None


def is_country_name(text: str | None) -> bool:
    """True when the whole string is nothing but a country name.

    Used to keep country names out of the victim column. "United States" is Title Case, sits
    next to a domain, and satisfies every test the organisation pattern applies — so without
    this it becomes a victim of its own, splitting one listing into two and taking the real
    victim's domain with it.
    """
    if not text:
        return False
    return text.strip().lower().strip(".,") in _COUNTRY_LOOKUP


def country_from_domain(domain: str | None) -> str | None:
    """Infer a country from a domain's ccTLD. None for generic and globally-sold TLDs.

    This is the route that actually populates the column, because listings print domains far
    more often than they print countries.
    """
    if not domain:
        return None

    labels = domain.strip().lower().rstrip(".").split(".")
    if len(labels) < 2:
        return None

    tld = labels[-1]
    if tld in GENERIC_CCTLDS:
        return None

    # The last label is the country code whatever the shape below it: `acme.co.uk`,
    # `acme.com.au` and `acme.de` all answer the same question.
    return CCTLD_COUNTRY.get(tld)


# Sector -> the words a listing uses when it says what a victim does. Matched against the
# victim's own name and the text around it, which is where this shows up in practice:
# "Northwind Medical Group", "City of Fairview", "Fairview Unified School District".
SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Healthcare": (
        "health", "healthcare", "hospital", "medical", "medicine", "clinic", "clinical",
        "dental", "dentistry", "surgery", "surgical", "care home", "nursing", "patient",
        "orthopedic", "orthopaedic", "pediatric", "paediatric", "radiology", "oncology",
    ),
    "Pharmaceuticals": (
        "pharma", "pharmaceutical", "pharmaceuticals", "pharmacy", "biotech",
        "biotechnology", "laboratories", "biosciences",
    ),
    "Financial Services": (
        "bank", "banking", "bancorp", "credit union", "capital", "finance", "financial",
        "investment", "investments", "asset management", "wealth", "brokerage",
        "securities", "mortgage", "lending", "payments", "fintech",
    ),
    "Insurance": ("insurance", "insurers", "assurance", "underwriters", "reinsurance"),
    "Legal": (
        "law firm", "law offices", "attorneys", "attorney", "solicitors", "barristers",
        "legal services", "advocates", "notary",
    ),
    "Education": (
        "school", "schools", "university", "universities", "college", "academy",
        "institute of technology", "school district", "campus", "education",
        "educational", "kindergarten", "polytechnic",
    ),
    "Government": (
        "ministry", "municipality", "municipal", "city of", "county of", "township",
        "government", "federal", "department of", "public works", "borough", "council",
        "administration", "parliament", "police", "sheriff", "fire department",
    ),
    "Manufacturing": (
        "manufacturing", "manufacturer", "industries", "industrial", "factory",
        "fabrication", "machining", "plastics", "steel", "foundry", "tooling",
        "components", "castings",
    ),
    "Automotive": (
        "automotive", "motors", "auto parts", "car dealership", "dealership", "vehicles",
        "trucking company", "tyres", "tires",
    ),
    "Aerospace & Defense": (
        "aerospace", "avionics", "defense", "defence", "aviation", "airlines", "airline",
        "airport", "aircraft", "space systems",
    ),
    "Technology": (
        "software", "technologies", "technology", "systems", "it services",
        "managed services", "cloud", "data center", "datacenter", "cybersecurity",
        "computing", "digital solutions", "hosting", "saas",
    ),
    "Telecommunications": (
        "telecom", "telecommunications", "telecoms", "broadband", "wireless", "mobile network",
        "isp", "internet service",
    ),
    "Energy & Utilities": (
        "energy", "utilities", "utility", "electric", "electricity", "power company",
        "oil", "gas", "petroleum", "solar", "renewables", "pipeline", "water authority",
        "grid",
    ),
    "Mining": ("mining", "minerals", "quarry", "quarries", "ore", "smelting"),
    "Construction": (
        "construction", "contractors", "contracting", "builders", "building services",
        "civil engineering", "roofing", "plumbing", "electrical services", "concrete",
        "architects", "architecture",
    ),
    "Real Estate": (
        "real estate", "realty", "properties", "property group", "estates", "letting",
        "housing association", "facilities management",
    ),
    "Transportation & Logistics": (
        "logistics", "freight", "shipping", "haulage", "transport", "transportation",
        "courier", "warehousing", "supply chain", "fleet", "maritime", "port authority",
        "rail", "railway",
    ),
    "Retail": (
        "retail", "retailer", "stores", "store", "supermarket", "shop", "boutique",
        "e-commerce", "ecommerce", "outlet", "wholesale", "distribution",
    ),
    "Food & Beverage": (
        "food", "foods", "beverage", "beverages", "brewery", "winery", "distillery",
        "dairy", "bakery", "catering", "restaurant", "restaurants", "meat", "produce",
    ),
    "Agriculture": (
        "agriculture", "agricultural", "agri", "farms", "farming", "livestock",
        "horticulture", "fisheries", "forestry",
    ),
    "Hospitality & Travel": (
        "hotel", "hotels", "resort", "resorts", "hospitality", "travel", "tourism",
        "casino", "leisure", "cruise",
    ),
    "Media & Entertainment": (
        "media", "broadcasting", "publishing", "publishers", "newspaper", "studios",
        "entertainment", "advertising", "marketing agency", "production company",
    ),
    "Professional Services": (
        "consulting", "consultants", "consultancy", "accounting", "accountants",
        "auditors", "advisory", "staffing", "recruitment", "engineering services",
        "surveyors",
    ),
    "Non-profit": (
        "foundation", "charity", "charitable", "non-profit", "nonprofit", "ngo",
        "association", "society", "trust fund", "humanitarian",
    ),
    "Chemicals": ("chemicals", "chemical", "coatings", "polymers", "fertilizer", "adhesives"),
}


def _build_sector_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        for keyword in keywords:
            lookup[keyword] = sector
    return lookup


_SECTOR_LOOKUP = _build_sector_lookup()

SECTOR_PATTERN = re.compile(_alternation(list(_SECTOR_LOOKUP)), re.IGNORECASE)


def parse_sector(raw: str | None) -> str | None:
    """Map a phrase to a sector. None when nothing in it indicates an industry."""
    if not raw:
        return None

    match = SECTOR_PATTERN.search(raw)
    return _SECTOR_LOOKUP.get(match.group(1).lower()) if match else None


def resolve_sector(candidates: list[str]) -> str | None:
    """Pick the best-supported sector from every phrase collected for one listing.

    Same shape as `resolve_status` and for the same reason: a victim named "Northwind
    Medical Transport" hits two sectors, and the one mentioned most across the listing's
    text is a better answer than whichever the extractor happened to see first.
    """
    scores: dict[str, int] = {}
    for raw in candidates:
        if not raw:
            continue
        for match in SECTOR_PATTERN.finditer(raw):
            sector = _SECTOR_LOOKUP.get(match.group(1).lower())
            if sector is not None:
                scores[sector] = scores.get(sector, 0) + 1

    if not scores:
        return None

    best = max(scores.values())
    # Alphabetical tie-break, so the same input always produces the same sector rather than
    # whichever ordering the dict happened to have.
    return sorted(sector for sector, score in scores.items() if score == best)[0]


def resolve_country(candidates: list[str], *, domain: str | None = None) -> str | None:
    """A listing's country: what it says if it says anything, else what its domain implies.

    Explicit text wins over the ccTLD. A German company on a `.com` is common; a listing
    that prints "Germany" and a domain that says otherwise means the site knows something
    the TLD does not.
    """
    for raw in candidates:
        country = parse_country(raw)
        if country is not None:
            return country
    return country_from_domain(domain)
