"""Settings, read from the repo-root .env — the same file the API and Drizzle use."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _find_repo_root() -> Path | None:
    """Walk up looking for the repo root.

    Deliberately not a fixed `parents[3]`: that is correct for
    `services/intel/intel/config.py` in a checkout, but the container copies the package to
    `/app/intel/`, where index 3 does not exist and the worker crashed on import with an
    IndexError before it could read a single setting.

    Returning None is a normal outcome, not a failure — in a container there is no .env and
    configuration arrives as real environment variables.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists() or (parent / "package.json").exists():
            return parent
    return None


REPO_ROOT = _find_repo_root()

if REPO_ROOT is not None:
    load_dotenv(REPO_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env") if REPO_ROOT is not None else None,
        extra="ignore",
    )

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # --- Tor ---
    # A list so the collector can round-robin. Running several Tor instances on separate
    # ports raises the effective circuit-rotation rate; one port is fine to start.
    tor_socks_ports: list[int] = Field(default=[9050], alias="TOR_SOCKS_PORTS")
    tor_host: str = Field(default="127.0.0.1", alias="TOR_HOST")

    # --- crawl politeness ---
    request_timeout_seconds: int = Field(default=60, alias="CRAWL_TIMEOUT")
    max_retries: int = Field(default=4, alias="CRAWL_RETRIES")
    # First retry delay, doubling up to the cap. Long on purpose: a Tor rendezvous circuit
    # takes this long to rebuild, and a shorter wait retries on the path that just failed.
    retry_backoff_seconds: int = Field(default=15, alias="CRAWL_RETRY_BACKOFF")
    retry_backoff_cap_seconds: int = Field(default=120, alias="CRAWL_RETRY_BACKOFF_CAP")
    # LEGACY: how many sources the old source-at-a-time crawler (`intel.pipeline.run_pipeline`,
    # used by `intel run`) works on at once. It means nothing to the URL-queue crawler below,
    # which is governed by CRAWL_WORKERS. Kept only until the CLI moves to crawl cycles.
    concurrency: int = Field(default=4, alias="CRAWL_CONCURRENCY")

    # Which crawler the scheduled and on-demand jobs use. `queue` is the URL-queue crawler
    # (`intel.crawl`): cycles, a Postgres frontier, concurrent workers. `legacy` is the
    # source-at-a-time crawler it replaces, kept as a switch-back that needs no redeploy of
    # code. The `intel run` CLI always uses the legacy crawler either way.
    engine: Literal["queue", "legacy"] = Field(default="queue", alias="CRAWL_ENGINE")
    # How often the cron looks for due work, in minutes. Nothing is fetched at this rate: a
    # tick only starts a cycle if some URL is due, and each URL has its own interval.
    sweep_interval_minutes: int = Field(
        default=5, ge=1, le=60, alias="CRAWL_SWEEP_INTERVAL_MINUTES"
    )

    # --- URL-queue crawler (`intel.crawl`) ---
    # The unit of work is a URL, not a source. These three numbers are SYSTEM-WIDE limits,
    # enforced in Postgres at claim time, not per process: running three worker processes
    # with CRAWL_WORKERS=6 still means at most 6 HTTP fetches in flight in total. Each process
    # runs enough claim loops to reach the limit and no more; the database is what says no.
    #
    # Deliberately not derived from the number of Tor SOCKS ports. All the ports belong to one
    # Tor process, so they separate circuit pools but add no throughput. Tune these from the
    # logged response times and failure rate, and from the Raspberry Pi's real memory.
    #
    # Simultaneous fetches of http-collector sources, across every worker process.
    workers: int = Field(default=6, ge=1, alias="CRAWL_WORKERS")
    # Simultaneous fetches of browser-collector sources. Firefox is ~500MB resident, so this
    # is bounded by RAM rather than by Tor.
    browser_workers: int = Field(default=1, ge=0, alias="CRAWL_BROWSER_WORKERS")
    # Simultaneous fetches of any ONE source, so a deep listing cannot take every slot and
    # lean on a single onion service.
    per_source_inflight: int = Field(default=3, ge=1, alias="CRAWL_PER_SOURCE_INFLIGHT")
    # Largest response body that is read. A body past this is abandoned mid-stream and the
    # URL fails permanently: leak-site listings are a few hundred KB, so anything near this
    # size is a file, not a page, and reading it only spends memory and Tor bandwidth.
    max_bytes: int = Field(default=5 * 1024 * 1024, alias="CRAWL_MAX_BYTES")
    # Hold a source's listing pages 2..N until page 1 of the same cycle has been resolved.
    # They are all queued up front either way; this only decides when they may be claimed.
    # On, a dead or gated source costs one failed page instead of N. Off, every page of every
    # source is attempted at once.
    page1_first: bool = Field(default=True, alias="CRAWL_PAGE1_FIRST")
    # Attempts per URL per cycle. A transient failure becomes a `retry` row with a backoff
    # time; the worker slot is released at once rather than slept on.
    max_attempts: int = Field(default=3, ge=1, alias="CRAWL_MAX_ATTEMPTS")
    # How long a claimed URL stays leased. Twice the request timeout by default (see
    # `lease_seconds`); a worker that dies mid-fetch is recovered when this runs out.
    lease_override_seconds: int = Field(default=0, alias="CRAWL_LEASE_SECONDS")
    # How long before a page reached by following a link is fetched again. A week: they are
    # one-off pages behind a listing entry, and are refetched sooner only when their parent
    # listing changes. Listing pages use each source's own intervals (`crawl_interval_seconds`
    # for page 1, `deep_crawl_interval_seconds` for the rest).
    link_recrawl_seconds: int = Field(default=604800, alias="CRAWL_LINK_INTERVAL")
    # Followed pages that have come due again, taken back into a cycle per source. Separate
    # from `link_max_pages`, which bounds links found for the first time, so a backlog of
    # recrawls cannot crowd out discovery of new victims.
    link_recrawl_max_pages: int = Field(default=25, alias="CRAWL_LINK_RECRAWL_MAX_PAGES")

    # How many pages of ONE source may be in flight together, so a ten-page listing does not
    # cost ten sequential Tor round trips. See `intel.scheduling.page_waves`.
    page_concurrency: int = Field(default=4, alias="CRAWL_PAGE_CONCURRENCY")
    # Ceiling on how large a single wave of simultaneous requests to one site may grow.
    page_wave_cap: int = Field(default=16, alias="CRAWL_PAGE_WAVE_CAP")

    # Total fetches in flight across every source, whatever `concurrency` and
    # `page_concurrency` multiply out to. Without this the two settings compose
    # multiplicatively (4 sources x 16-page waves = 64 simultaneous circuits) and Tor
    # becomes the bottleneck for every one of them. 0 means "derive it".
    max_inflight_fetches: int = Field(default=0, alias="CRAWL_MAX_INFLIGHT")

    # --- fair scheduling ---
    # The wall-clock window one run aims to finish inside, however many sources are due. Each
    # source is given `window * concurrency / sources` seconds (see
    # `intel.scheduling.source_time_budget`), so 10 sources and 100 sources both fit.
    run_window_seconds: int = Field(default=1800, alias="CRAWL_RUN_WINDOW")
    # What one source needs to answer page 1 over Tor at all. The per-source share never goes
    # below this; if that means the run overruns its window the pipeline says so.
    source_min_budget_seconds: int = Field(default=90, alias="CRAWL_SOURCE_MIN_BUDGET")
    # Upper bound on one source's share, so a run with few sources does not let one of them
    # dig for the whole window.
    source_max_budget_seconds: int = Field(default=900, alias="CRAWL_SOURCE_MAX_BUDGET")

    # --- link following (the "tree" of a leak site) ---
    # After a deep walk, follow links from the listing into the pages behind it — victim
    # pages, proof pages — looking for exposures and new mirrors. Same host only, and never
    # a file (see `collectors/links.py`).
    follow_links: bool = Field(default=True, alias="CRAWL_FOLLOW_LINKS")
    # How many levels down the tree to go. 3 reaches listing -> victim page -> its sub-pages.
    link_depth: int = Field(default=3, alias="CRAWL_LINK_DEPTH")
    # How many links to take from each page. Bounds the fan-out at every level.
    links_per_page: int = Field(default=5, alias="CRAWL_LINKS_PER_PAGE")
    # Hard cap on followed pages per source per crawl. Depth and fan-out alone allow
    # 5 + 25 + 125 pages; this is what stops a big site becoming a hundred Tor fetches.
    link_max_pages: int = Field(default=25, alias="CRAWL_LINK_MAX_PAGES")

    # How long a full scheduled run may take. The default was arq's 300s, which is far less
    # than the ~15 minutes 32 sources need, so every scheduled crawl was killed mid-run and
    # the system only ever collected anything when someone ran the CLI by hand.
    job_timeout_seconds: int = Field(default=3600, alias="CRAWL_JOB_TIMEOUT")

    # --- hunts ---
    # How long a company search may run before it is treated as abandoned. Far shorter than
    # a crawl's hour: a hunt is four HTTPS lookups with their own timeouts, so anything past
    # this is a dead worker, not a slow registry.
    hunt_timeout_seconds: int = Field(default=120, alias="HUNT_TIMEOUT")

    # --- background enrichment sweep ---
    # How many domains one tick enriches. Small on purpose: the sweep fills a backlog over
    # hours, and finishing it quickly would mean a burst of outbound requests that looks
    # exactly like a scan to everyone on the receiving end.
    enrich_batch_size: int = Field(default=12, alias="ENRICH_BATCH")
    # Simultaneous third-party servers. The whole politeness budget of the sweep.
    enrich_concurrency: int = Field(default=4, alias="ENRICH_CONCURRENCY")
    # How long an enrichment row stays usable before the sweep refreshes it. A day: WHOIS
    # barely moves, and site status is the only genuinely volatile field.
    enrich_max_age_seconds: int = Field(default=86400, alias="ENRICH_MAX_AGE")
    # Indicator hosts get WHOIS only (see `enrich_sweep`), a batch per tick alongside the
    # victim domains. Refreshed weekly: there are thousands of them, and a registration
    # record does not change on the timescale a site's status does.
    enrich_ioc_batch_size: int = Field(default=24, alias="ENRICH_IOC_BATCH")
    enrich_ioc_max_age_seconds: int = Field(default=7 * 86400, alias="ENRICH_IOC_MAX_AGE")

    # --- indicator feeds ---
    # Whether to fetch public IOC feeds at all. Off is a legitimate posture: it is outbound
    # traffic to third parties on a timer, and some deployments would rather not.
    feeds_enabled: bool = Field(default=True, alias="FEEDS_ENABLED")
    # Indicators taken per feed per run. These dumps hold weeks of history; ingesting all of
    # it on the first run would stamp thousands of old indicators as arriving at once.
    feeds_max_entries: int = Field(default=4000, alias="FEEDS_MAX_ENTRIES")

    # --- scam phone-number reports (Bulk Intelligence · Mobile Number) ---
    # Public posts people write about numbers that scammed them, read from Mastodon hashtag
    # timelines and subreddit RSS. Governed by FEEDS_ENABLED as well: it is the same kind of
    # timed outbound traffic.
    mobile_enabled: bool = Field(default=True, alias="MOBILE_ENABLED")
    # Any Mastodon server works; mastodon.social serves public tag timelines without a login
    # and sees posts federated from every other server.
    mobile_mastodon_instance: str = Field(
        default="https://mastodon.social", alias="MOBILE_MASTODON_INSTANCE"
    )
    mobile_mastodon_tags: list[str] = Field(
        default=[
            "scam",
            "scammer",
            "scammers",
            "scamcall",
            "scamcalls",
            "phonescam",
            "smishing",
            "vishing",
            "scamalert",
            "spamcall",
            "textscam",
            "whatsappscam",
            "fraud",
        ],
        alias="MOBILE_MASTODON_TAGS",
    )
    mobile_subreddits: list[str] = Field(default=["Scams"], alias="MOBILE_SUBREDDITS")

    # --- mirror discovery ---
    # Record onion addresses mentioned on crawled pages. Recording is always safe; it is
    # only ever data until something acts on it.
    discover_mirrors: bool = Field(default=True, alias="CRAWL_DISCOVER_MIRRORS")
    # Fall back to a discovered address when a source's primary one is dead. Off by default:
    # these addresses come from pages served by the sites being crawled, so switching to one
    # automatically lets a crawled host choose where the crawler connects. Turn it on when
    # you want unattended continuity and have accepted that trade.
    mirror_failover: bool = Field(default=False, alias="CRAWL_MIRROR_FAILOVER")

    # --- exposure detection ---
    # Look for credentials, keys and card numbers in crawled pages. Findings are stored masked,
    # never in the clear (see `extract/secrets.py`), so this is safe to leave on.
    exposure_detection: bool = Field(default=True, alias="EXPOSURE_DETECTION")
    # Key for the HMAC that fingerprints a finding. It is what stops a database dump from being
    # tested against a dictionary of common emails and passwords, so a real deployment should
    # set its own. Changing it later re-keys every future finding: old rows stop matching and
    # a re-crawl inserts them again.
    exposure_salt: str = Field(default="leakmon-dev-salt-change-me", alias="EXPOSURE_SALT")

    extractor: str = Field(default="rules", alias="INTEL_EXTRACTOR")

    sources_file: Path = Field(default=SERVICE_ROOT / "sources.yaml", alias="INTEL_SOURCES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def lease_seconds(self) -> int:
        """How long a claimed URL is leased before the reaper may take it back."""
        if self.lease_override_seconds > 0:
            return self.lease_override_seconds
        return max(30, self.request_timeout_seconds * 2)

    @property
    def fetch_budget(self) -> int:
        """Hard ceiling on simultaneous fetches across the whole run.

        Derived rather than required, because the useful value is a function of the other
        two settings and nobody should have to keep three numbers consistent by hand. The
        derived value is deliberately smaller than `concurrency * page_concurrency`: not
        every source is mid-wave at the same moment, so budgeting for the worst case just
        means the budget never binds and Tor takes the overload instead.
        """
        if self.max_inflight_fetches > 0:
            return self.max_inflight_fetches
        return max(self.concurrency, self.concurrency + self.page_concurrency)

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg wants postgresql://, not the postgres:// some tools emit."""
        dsn = self.database_url
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://") :]
        return dsn


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
