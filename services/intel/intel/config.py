"""Settings, read from the repo-root .env — the same file the API and Drizzle use."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    # First retry delay, doubling up to the cap. The old backoff was 2s/4s/8s, which is far
    # shorter than a Tor rendezvous circuit takes to rebuild — so every retry reused a path
    # that had just failed and the crawler gave up on sites that were merely slow.
    retry_backoff_seconds: int = Field(default=15, alias="CRAWL_RETRY_BACKOFF")
    retry_backoff_cap_seconds: int = Field(default=120, alias="CRAWL_RETRY_BACKOFF_CAP")
    # How many sources to crawl at once. The old crawler was strictly sequential, so 83
    # sources at ~20s per page took many hours per cycle.
    concurrency: int = Field(default=4, alias="CRAWL_CONCURRENCY")

    # How many pages of ONE source may be in flight together. Pages used to be walked
    # strictly in order, so a ten-page listing cost ten sequential Tor round trips whatever
    # the cross-source concurrency was. See `intel.scheduling.page_waves`.
    page_concurrency: int = Field(default=4, alias="CRAWL_PAGE_CONCURRENCY")
    # Ceiling on how large a single wave of simultaneous requests to one site may grow.
    page_wave_cap: int = Field(default=16, alias="CRAWL_PAGE_WAVE_CAP")

    # Total fetches in flight across every source, whatever `concurrency` and
    # `page_concurrency` multiply out to. Without this the two settings compose
    # multiplicatively (4 sources x 16-page waves = 64 simultaneous circuits) and Tor
    # becomes the bottleneck for every one of them. 0 means "derive it".
    max_inflight_fetches: int = Field(default=0, alias="CRAWL_MAX_INFLIGHT")

    # How long a full scheduled run may take. The default was arq's 300s, which is far less
    # than the ~15 minutes 32 sources need, so every scheduled crawl was killed mid-run and
    # the system only ever collected anything when someone ran the CLI by hand.
    job_timeout_seconds: int = Field(default=3600, alias="CRAWL_JOB_TIMEOUT")

    # --- mirror discovery ---
    # Record onion addresses mentioned on crawled pages. Recording is always safe; it is
    # only ever data until something acts on it.
    discover_mirrors: bool = Field(default=True, alias="CRAWL_DISCOVER_MIRRORS")
    # Fall back to a discovered address when a source's primary one is dead. Off by default:
    # these addresses come from pages served by the sites being crawled, so switching to one
    # automatically lets a crawled host choose where the crawler connects. Turn it on when
    # you want unattended continuity and have accepted that trade.
    mirror_failover: bool = Field(default=False, alias="CRAWL_MIRROR_FAILOVER")

    extractor: str = Field(default="rules", alias="INTEL_EXTRACTOR")

    sources_file: Path = Field(default=SERVICE_ROOT / "sources.yaml", alias="INTEL_SOURCES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

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
