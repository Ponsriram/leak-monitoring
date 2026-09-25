"""Judge whether a URL is worth adding to sources.yaml.

`intel sources probe <url>` fetches one page the way a crawl would and runs it through the
same checks — gate detection, the empty-page floor, extraction — then answers the questions
a person otherwise answers by clicking around in Tor Browser: is this a leak listing, which
collector does it need, how does it paginate, has it announced a move.

Nothing here writes to the database. The evaluation is pure so it can be tested against
saved pages without Tor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .collectors import classify_onion_urls, onion_host, to_text
from .models import ExtractedLeak
from .pipeline import MIN_PAGE_TEXT_CHARS, extract_page, gate_kind

# Fewer extracted victims than this and the page is not a listing, or not one the extractor
# can read. Real listings seen on 2026-09-18 ranged from 6 to 766 on page 1.
MIN_VICTIMS = 5

# Vocabulary of forums, markets and shops. Each was on the page 1 of a source that turned out
# not to be a leak site; the extractor turns their thread titles and product names into
# fake victims. Two or more distinct markers is a strong hint.
_OFF_TOPIC = re.compile(
    r"\b(new posts|all threads|latest threads|threads|replies|forums?|karma|"
    r"add to cart|shopping cart|vendors?|marketplace|escrow|products|processed orders|"
    r"reship|refund policy)\b",
    re.I,
)
_OFF_TOPIC_THRESHOLD = 2

# Link text or path that points at a victim list. Offered when the probed URL is not one,
# because the usual mistake is a base_url one click away from the listing (lynx: /leak is
# the news page, /leaks the list).
_LISTING_LINK = re.compile(
    r"leak|disclos|victim|compan|publication|blog|data|news|press|release", re.I
)

_PAGINATION = (
    ("query", re.compile(r"[?&]page=2\b")),
    ("path", re.compile(r"/page/2/?(?:$|[?#])")),
    ("offset", re.compile(r"[?&]offset=\d+")),
)


@dataclass(slots=True)
class ProbeReport:
    url: str
    collector: str
    reachable: bool
    error: str | None = None
    html_bytes: int = 0
    text: str = ""
    gate: str | None = None
    victims: list[ExtractedLeak] = field(default_factory=list)
    off_topic: list[str] = field(default_factory=list)
    announced_mirrors: list[str] = field(default_factory=list)
    # Every other onion address on the page. Not all are this site's — negotiation portals,
    # other crews — but a move notice worded unlike the announcement patterns lands here.
    other_onions: list[str] = field(default_factory=list)
    pagination: str = "none"
    listing_links: list[str] = field(default_factory=list)

    @property
    def too_short(self) -> bool:
        return len(self.text.strip()) < MIN_PAGE_TEXT_CHARS

    @property
    def usable(self) -> bool:
        """Would a crawl of this page collect real victims?"""
        return (
            self.reachable
            and self.gate is None
            and not self.too_short
            and len(self.victims) >= MIN_VICTIMS
            and not self.off_topic_site
        )

    @property
    def off_topic_site(self) -> bool:
        """A forum, market or shop. Neither a browser nor another path on it will help."""
        return len(self.off_topic) >= _OFF_TOPIC_THRESHOLD

    def problems(self) -> list[str]:
        """Each failed check, in the order they block a crawl."""
        if not self.reachable:
            return [f"unreachable: {self.error or 'no response'}"]
        found: list[str] = []
        if self.gate:
            found.append(f"gated: page 1 is a {self.gate} page — the crawler cannot pass it")
        elif self.too_short:
            found.append(
                f"near-empty: {len(self.text.strip())} chars of text — JS-rendered or a challenge"
            )
        if self.off_topic_site:
            found.append(
                "looks like a forum/market/shop, not a leak site: " + ", ".join(self.off_topic)
            )
        if not self.gate and not self.too_short and len(self.victims) < MIN_VICTIMS:
            found.append(
                f"only {len(self.victims)} victim(s) extracted — not a listing page, or a "
                "layout the extractor cannot read"
            )
        return found


def evaluate(url: str, collector: str, html: str | None, error: str | None) -> ProbeReport:
    """Run every check on one fetched page."""
    report = ProbeReport(url=url, collector=collector, reachable=html is not None, error=error)
    if html is None:
        return report

    report.html_bytes = len(html.encode("utf-8"))
    report.text = to_text(html)
    report.gate = gate_kind(report.text)
    if report.gate is None and not report.too_short:
        report.victims = extract_page(
            report.text, source_group="probe", source_url=url, page_no=1, extractor_name="rules"
        )

    report.off_topic = sorted({m.group(1).lower() for m in _OFF_TOPIC.finditer(report.text)})

    own = onion_host(url)
    announced, other = classify_onion_urls(report.text, exclude_hosts={own} if own else set())
    report.announced_mirrors = list(announced.values())
    report.other_onions = list(other.values())

    hrefs = _same_site_links(html, url)
    report.pagination = next(
        (style for style, pattern in _PAGINATION if any(pattern.search(h) for h in hrefs)),
        "none",
    )
    here = urlparse(url).path.rstrip("/")
    report.listing_links = sorted(
        {
            h
            for h in hrefs
            if _LISTING_LINK.search(urlparse(h).path) and urlparse(h).path.rstrip("/") != here
        }
    )[:10]
    return report


def _same_site_links(html: str, base: str) -> list[str]:
    """Absolute URLs of every link on the page that stays on the probed host."""
    host = urlparse(base).netloc
    links: list[str] = []
    for node in HTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(base, href)
        if urlparse(absolute).netloc == host:
            links.append(absolute)
    return links


def yaml_snippet(url: str, collector: str, pagination: str) -> str:
    """A sources.yaml entry for a page that passed. Slug and name are for the human to fix."""
    host = onion_host(url) or urlparse(url).netloc
    slug = host[:12]
    return (
        f"- slug: {slug}\n"
        f"  name: {slug}  # rename to the group's name as the page shows it\n"
        f'  base_url: "{url}"\n'
        f"  collector: {collector}\n"
        f"  pagination_style: {pagination}\n"
        f"  max_pages: 5\n"
        f"  crawl_interval_seconds: 900\n"
        f"  request_delay_seconds: 15\n"
        f"  enabled: false"
    )
