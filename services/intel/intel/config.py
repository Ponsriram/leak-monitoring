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
    max_retries: int = Field(default=3, alias="CRAWL_RETRIES")
    # How many sources to crawl at once. The old crawler was strictly sequential, so 83
    # sources at ~20s per page took many hours per cycle.
    concurrency: int = Field(default=4, alias="CRAWL_CONCURRENCY")

    extractor: str = Field(default="rules", alias="INTEL_EXTRACTOR")

    sources_file: Path = Field(default=SERVICE_ROOT / "sources.yaml", alias="INTEL_SOURCES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

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
