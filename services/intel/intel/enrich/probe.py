"""One HTTP request against a victim's home page: is it up, and what is it built on.

This fills two columns the reference consoles carry and we had nothing for — Site Status and
Web Technologies — and it does it with a single ordinary GET. Not a scan, not a crawl, not a
sweep of paths: exactly the request a browser makes when someone types the domain, which is
the line between passive research and probing somebody's infrastructure.

Everything is inferred from what that one response already contains: the status line, the
response headers, the cookies the server sets, and the markup it returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
import structlog
from selectolax.parser import HTMLParser

log = structlog.get_logger(__name__)

__all__ = ["ProbeResult", "probe_domain"]

# Enough of the page to carry the <head>, where nearly every fingerprint lives, without
# pulling a multi-megabyte single-page-app bundle into memory for a chip that says "React".
_MAX_BODY_BYTES = 512_000


@dataclass(slots=True)
class ProbeResult:
    """What the probe saw. `status` maps 1:1 onto the `site_status` enum in the database."""

    status: str = "not_scanned"
    http_status: int | None = None
    technologies: list[str] = field(default_factory=list)
    page_title: str | None = None
    final_url: str | None = None
    error: str | None = None


# --- fingerprints -------------------------------------------------------------------------
#
# Three signal kinds, because the evidence genuinely lives in three places and no single one
# covers the common stack:
#
#   header   the server tells you outright (`Server: nginx`, `X-Powered-By: PHP/8.2`)
#   cookie   the framework's session cookie name is often the only trace it leaves
#   body     markup markers — a meta generator, a script src, a stylesheet path
#
# The list is deliberately conservative. A wrong chip is worse than a missing one: an analyst
# who sees "WordPress" on a site that does not run it will waste time on WordPress
# vulnerabilities, so each pattern here is one that does not fire on anything else. Broad
# guesses ("this looks like PHP because the URL ends in .php") are left out on purpose.

_HEADER_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("Nginx", "server", re.compile(r"nginx", re.I)),
    ("Apache HTTP Server", "server", re.compile(r"apache", re.I)),
    ("LiteSpeed", "server", re.compile(r"litespeed", re.I)),
    ("Microsoft IIS", "server", re.compile(r"microsoft-iis", re.I)),
    ("Caddy", "server", re.compile(r"caddy", re.I)),
    ("Cloudflare", "server", re.compile(r"cloudflare", re.I)),
    ("Cloudflare", "cf-ray", re.compile(r".")),
    ("Amazon CloudFront", "via", re.compile(r"cloudfront", re.I)),
    ("Amazon CloudFront", "x-amz-cf-id", re.compile(r".")),
    ("Fastly", "x-served-by", re.compile(r"cache-", re.I)),
    ("Akamai", "x-akamai-transformed", re.compile(r".")),
    ("Varnish", "via", re.compile(r"varnish", re.I)),
    ("Imperva", "x-iinfo", re.compile(r".")),
    ("Sucuri", "x-sucuri-id", re.compile(r".")),
    ("PHP", "x-powered-by", re.compile(r"php", re.I)),
    ("ASP.NET", "x-powered-by", re.compile(r"asp\.net", re.I)),
    ("ASP.NET", "x-aspnet-version", re.compile(r".")),
    ("Express", "x-powered-by", re.compile(r"express", re.I)),
    ("Next.js", "x-powered-by", re.compile(r"next\.js", re.I)),
    ("Ubuntu", "server", re.compile(r"ubuntu", re.I)),
    ("Debian", "server", re.compile(r"debian", re.I)),
    ("OpenSSL", "server", re.compile(r"openssl", re.I)),
    ("Shopify", "x-shopid", re.compile(r".")),
    ("WP Engine", "x-powered-by", re.compile(r"wp engine", re.I)),
    ("HSTS", "strict-transport-security", re.compile(r".")),
    ("Drupal", "x-generator", re.compile(r"drupal", re.I)),
    ("Drupal", "x-drupal-cache", re.compile(r".")),
)

_COOKIE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PHP", re.compile(r"^PHPSESSID$", re.I)),
    ("WordPress", re.compile(r"^wordpress_", re.I)),
    ("WooCommerce", re.compile(r"^woocommerce_", re.I)),
    ("Laravel", re.compile(r"^laravel_session$", re.I)),
    ("ASP.NET", re.compile(r"^ASP\.NET_SessionId$", re.I)),
    ("Java", re.compile(r"^JSESSIONID$", re.I)),
    ("Django", re.compile(r"^(csrftoken|django_language)$", re.I)),
    ("Shopify", re.compile(r"^_shopify_", re.I)),
    ("Cloudflare Bot Management", re.compile(r"^__cf_bm$", re.I)),
)

_BODY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WordPress", re.compile(r"/wp-(content|includes)/", re.I)),
    ("Elementor", re.compile(r"/plugins/elementor/|elementor-frontend", re.I)),
    ("Yoast SEO", re.compile(r"/wordpress-seo/|yoast\s+seo\s+plugin", re.I)),
    (
        "WooCommerce",
        re.compile(r"/plugins/woocommerce/|woocommerce-(?:layout|smallscreen|general)", re.I),
    ),
    ("Site Kit", re.compile(r"google-site-kit", re.I)),
    ("Joomla", re.compile(r"/media/jui/|/media/system/js/|joomla-script-options", re.I)),
    ("Drupal", re.compile(r"/sites/(all|default)/|drupal\.js", re.I)),
    ("Magento", re.compile(r"/static/version\d+/frontend/|Mage\.Cookies|/mage/cookies", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com", re.I)),
    ("Wix", re.compile(r"static\.parastorage\.com|wixstatic\.com", re.I)),
    ("Squarespace", re.compile(r"static1\.squarespace\.com|squarespace\.com/universal", re.I)),
    ("Webflow", re.compile(r"assets\.website-files\.com|webflow\.js|data-wf-page", re.I)),
    ("jQuery", re.compile(r"jquery(?:[.-]min)?\.js", re.I)),
    ("jQuery Migrate", re.compile(r"jquery-migrate", re.I)),
    ("Bootstrap", re.compile(r"bootstrap(?:\.min)?\.(?:css|js)", re.I)),
    ("React", re.compile(r"__NEXT_DATA__|data-reactroot|react(?:-dom)?(?:\.min)?\.js", re.I)),
    ("Vue.js", re.compile(r"vue(?:\.min|\.runtime)?\.js|data-v-[0-9a-f]{8}", re.I)),
    ("Angular", re.compile(r"ng-version=|angular(?:\.min)?\.js", re.I)),
    ("Next.js", re.compile(r"/_next/static/", re.I)),
    ("Nuxt", re.compile(r"/_nuxt/", re.I)),
    (
        "Google Analytics",
        re.compile(r"google-analytics\.com|gtag\('config'|googletagmanager\.com/gtag", re.I),
    ),
    ("Google Tag Manager", re.compile(r"googletagmanager\.com/gtm", re.I)),
    ("Google Font API", re.compile(r"fonts\.googleapis\.com", re.I)),
    ("Google Maps", re.compile(r"maps\.googleapis\.com", re.I)),
    ("Adobe Fonts", re.compile(r"use\.typekit\.net", re.I)),
    ("Font Awesome", re.compile(r"font-?awesome[^\"']*\.(?:css|js)|fontawesome\.com", re.I)),
    ("Facebook Pixel", re.compile(r"connect\.facebook\.net/[^\"']*fbevents", re.I)),
    ("reCAPTCHA", re.compile(r"google\.com/recaptcha", re.I)),
    ("Cloudflare Browser Insights", re.compile(r"static\.cloudflareinsights\.com", re.I)),
    ("jsDelivr", re.compile(r"cdn\.jsdelivr\.net", re.I)),
    ("cdnjs", re.compile(r"cdnjs\.cloudflare\.com", re.I)),
    ("Open Graph", re.compile(r"property=[\"']og:", re.I)),
    ("RSS", re.compile(r"type=[\"']application/rss\+xml", re.I)),
    ("HubSpot", re.compile(r"js\.hs-scripts\.com", re.I)),
    ("Hotjar", re.compile(r"static\.hotjar\.com", re.I)),
    ("Matomo", re.compile(r"matomo\.js|piwik\.js", re.I)),
)

# Meta tags naming the product outright, e.g.
# `<meta name="generator" content="Joomla! - ...">`.
_GENERATOR_RE = re.compile(
    r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']{1,120})[\"']", re.I
)
_GENERATOR_PRODUCTS = ("WordPress", "Joomla", "Drupal", "TYPO3", "Hugo", "Jekyll", "Ghost")


def _fingerprint(response: httpx.Response, body: str) -> list[str]:
    found: set[str] = set()

    for name, header, pattern in _HEADER_RULES:
        value = response.headers.get(header)
        if value and pattern.search(value):
            found.add(name)

    # `set-cookie` legitimately repeats, and httpx's mapping collapses duplicates on lookup —
    # `get_list` is the only way to see every cookie a server set, and the framework-revealing
    # one is rarely the first.
    for raw_cookie in response.headers.get_list("set-cookie"):
        cookie_name = raw_cookie.split("=", 1)[0].strip()
        for name, pattern in _COOKIE_RULES:
            if pattern.match(cookie_name):
                found.add(name)

    for name, pattern in _BODY_RULES:
        if pattern.search(body):
            found.add(name)

    generator = _GENERATOR_RE.search(body)
    if generator:
        content = generator.group(1)
        for product in _GENERATOR_PRODUCTS:
            if product.lower() in content.lower():
                found.add(product)

    if response.http_version == "HTTP/2":
        found.add("HTTP/2")
    elif response.http_version == "HTTP/3":
        found.add("HTTP/3")

    return sorted(found)


def _title(body: str) -> str | None:
    try:
        node = HTMLParser(body).css_first("title")
    except Exception:  # noqa: BLE001 - a malformed page must not fail the probe
        return None
    if node is None:
        return None
    text = (node.text() or "").strip()
    return text[:200] or None


async def probe_domain(client: httpx.AsyncClient, domain: str) -> ProbeResult:
    """Fetch `https://<domain>/` once and report what came back.

    HTTPS only, and deliberately no plaintext fallback: a site that answers only on port 80
    in 2026 is a finding in itself, and trying both would double the requests we make against
    someone else's server to improve a status chip.

    Failure modes are separated because they mean different things to an analyst. `down` is
    "we asked and the site is not serving" — refused connections, DNS that does not resolve,
    timeouts, 5xx. `error` is "our probe broke", which is our problem and not evidence about
    the victim. Folding the second into the first would quietly report healthy sites as down
    whenever egress was blocked.
    """
    url = f"https://{domain}/"
    try:
        response = await client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        log.debug("probe: unreachable", domain=domain, error=str(exc))
        return ProbeResult(status="down", error=type(exc).__name__)
    except httpx.HTTPError as exc:
        log.debug("probe: transport error", domain=domain, error=str(exc))
        return ProbeResult(status="error", error=f"{type(exc).__name__}: {exc}"[:300])

    # A 5xx is the server admitting it cannot serve the site; 4xx (401, 403, 404) still means
    # something is listening and answering, which is what "Live" claims.
    status = "down" if response.status_code >= 500 else "live"

    body = response.text[:_MAX_BODY_BYTES] if response.content else ""

    return ProbeResult(
        status=status,
        http_status=response.status_code,
        technologies=_fingerprint(response, body),
        page_title=_title(body),
        final_url=str(response.url),
    )
