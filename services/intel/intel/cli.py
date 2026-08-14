"""`intel` — the command line for the collection pipeline.

Replaces the notebook ritual. Every operation that used to mean "run these cells in this
order" is a command that can be scripted, scheduled, and re-run safely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
import typer
import yaml

from .config import get_settings
from .logging import configure_logging
from .pipeline import extract_page, run_pipeline
from .storage import Storage

app = typer.Typer(
    add_completion=False,
    help="Collection and extraction pipeline for ransomware leak-site monitoring.",
)
sources_app = typer.Typer(help="Manage monitored sources.")
app.add_typer(sources_app, name="sources")

log = structlog.get_logger(__name__)


def _setup() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)


async def _with_storage(coro):  # type: ignore[no-untyped-def]
    settings = get_settings()
    storage = await Storage.connect(settings.asyncpg_dsn)
    try:
        return await coro(storage, settings)
    finally:
        await storage.close()


# ---------------------------------------------------------------- sources


@sources_app.command("sync")
def sources_sync(
    file: Path | None = typer.Option(None, help="Path to sources.yaml"),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Also DELETE sources absent from the file (drops crawl history; leaks survive)",
    ),
) -> None:
    """Load sources.yaml into the database.

    Without --prune this only adds and updates, so sources removed from the file linger in
    the database. That is the safe default: deleting a source also deletes its crawl runs
    and fetched pages.
    """
    _setup()
    settings = get_settings()
    path = file or settings.sources_file

    definitions = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(definitions, list):
        typer.echo("sources.yaml must contain a list of sources", err=True)
        raise typer.Exit(1)

    slugs = [item["slug"] for item in definitions]

    async def work(storage: Storage, _: object) -> tuple[int, int, list[str]]:
        ins, upd = await storage.sync_sources(definitions)
        removed = await storage.prune_sources(slugs) if prune else []
        return ins, upd, removed

    inserted, updated, removed = asyncio.run(_with_storage(work))
    typer.echo(f"Synced {len(definitions)} sources: {inserted} added, {updated} updated.")
    if removed:
        typer.echo(f"Pruned {len(removed)}: {', '.join(sorted(removed))}")
    elif not prune:
        typer.echo("(pass --prune to delete sources no longer listed in the file)")


@sources_app.command("list")
def sources_list(
    all_sources: bool = typer.Option(False, "--all", help="Include disabled sources"),
) -> None:
    """Show sources and their crawl health."""
    _setup()

    async def work(storage: Storage, _: object) -> list:  # type: ignore[type-arg]
        return await storage.list_sources(only_enabled=not all_sources)

    rows = asyncio.run(_with_storage(work))
    if not rows:
        typer.echo("No sources. Run `intel sources sync` first.")
        return

    typer.echo(f"{'SLUG':<22} {'COLLECTOR':<10} {'ON':<4} {'FAILS':<6} LAST CRAWL")
    for row in rows:
        last = row.last_crawl_at.strftime("%Y-%m-%d %H:%M") if row.last_crawl_at else "never"
        typer.echo(
            f"{row.slug:<22} {row.collector:<10} "
            f"{'yes' if row.enabled else 'no':<4} {row.consecutive_failures:<6} {last}"
        )


@sources_app.command("enable")
def sources_enable(
    slug: str | None = typer.Argument(None, help="Source slug, or use --all"),
    all_sources: bool = typer.Option(False, "--all", help="Enable every source"),
) -> None:
    """Enable crawling for a source.

    Sources ship DISABLED. Crawling live ransomware infrastructure is a deliberate act that
    needs Tor running and needs you to have considered the legal and operational position —
    so it is opt-in rather than something that starts the moment you sync a config file.
    """
    _setup()
    if not slug and not all_sources:
        typer.echo("Give a slug or --all", err=True)
        raise typer.Exit(1)

    async def work(storage: Storage, _: object) -> int:
        if all_sources:
            return await storage._pool.fetchval(  # noqa: SLF001 - admin command
                "with u as (update sources set enabled = true where not enabled returning 1) "
                "select count(*) from u"
            )
        return await storage._pool.fetchval(  # noqa: SLF001
            "with u as (update sources set enabled = true where slug = $1 returning 1) "
            "select count(*) from u",
            slug,
        )

    count = asyncio.run(_with_storage(work))
    typer.echo(f"Enabled {count} source(s).")


@sources_app.command("disable")
def sources_disable(slug: str = typer.Argument(..., help="Source slug")) -> None:
    """Stop crawling a source."""
    _setup()

    async def work(storage: Storage, _: object) -> int:
        return await storage._pool.fetchval(  # noqa: SLF001
            "with u as (update sources set enabled = false where slug = $1 returning 1) "
            "select count(*) from u",
            slug,
        )

    count = asyncio.run(_with_storage(work))
    typer.echo(f"Disabled {count} source(s).")


# ---------------------------------------------------------------- run


@app.command("run")
def run(
    source: list[str] | None = typer.Option(
        None, "--source", "-s", help="Limit to these slugs (repeatable)"
    ),
    extractor: str | None = typer.Option(None, help="rules | gliner"),
) -> None:
    """Crawl every enabled source, extract, and load.

    Safe to re-run: unchanged pages are skipped by content hash, and leaks upsert on
    dedupe_hash rather than inserting duplicates.
    """
    _setup()

    async def work(storage: Storage, settings: object):  # type: ignore[no-untyped-def]
        return await run_pipeline(
            storage=storage,
            settings=settings,  # type: ignore[arg-type]
            slugs=list(source) if source else None,
            extractor_name=extractor,
        )

    result = asyncio.run(_with_storage(work))
    typer.echo(
        f"Done. {result.inserted} new leaks, {result.updated} seen again, "
        f"{len(result.failed)} source(s) failed."
    )
    if result.failed:
        typer.echo(f"Failed: {', '.join(result.failed)}", err=True)


@app.command("extract-file")
def extract_file(
    path: Path = typer.Argument(..., help="A text file to extract from"),
    group: str = typer.Option("unknown", help="Ransomware group slug for these listings"),
    extractor: str = typer.Option("rules", help="rules | gliner"),
    load: bool = typer.Option(False, "--load", help="Write results to the database"),
) -> None:
    """Extract leaks from a local text file.

    This is the offline path for testing an extractor against saved page text without
    touching Tor — and the migration path for the old `combined_output_clean_text.txt`.
    """
    _setup()
    text = path.read_text(encoding="utf-8", errors="replace")

    leaks = extract_page(
        text,
        source_group=group,
        source_url=None,
        page_no=1,
        extractor_name=extractor,
    )

    typer.echo(f"Extracted {len(leaks)} leak(s) from {path.name}:")
    for leak in leaks:
        # str() first: `date.__format__` treats a non-empty spec as a strftime pattern, so
        # `f"{some_date:<12}"` renders the literal text "<12" rather than padding.
        published = str(leak.published_at.date()) if leak.published_at else "-"
        typer.echo(
            f"  {leak.victim_name or '(no name)':<40} "
            f"{leak.victim_domain or '-':<28} "
            f"{published:<12} "
            f"{leak.status.value}"
        )

    if not load:
        typer.echo("\nDry run. Pass --load to write these to the database.")
        return

    async def work(storage: Storage, _: object):  # type: ignore[no-untyped-def]
        row = await storage.get_source(group)
        return await storage.upsert_leaks(leaks, source_id=row.id if row else None)

    result = asyncio.run(_with_storage(work))
    typer.echo(
        f"Loaded: {result.inserted} new, {result.updated} updated, {result.skipped} skipped."
    )


@app.command("status")
def status() -> None:
    """Show what is in the database."""
    _setup()

    async def work(storage: Storage, _: object) -> tuple[int, int, int]:
        total = await storage.count_leaks()
        enabled = len(await storage.list_sources(only_enabled=True))
        all_sources = len(await storage.list_sources(only_enabled=False))
        return total, enabled, all_sources

    total, enabled, all_sources = asyncio.run(_with_storage(work))
    typer.echo(f"Leaks:   {total}")
    typer.echo(f"Sources: {enabled} enabled / {all_sources} total")


if __name__ == "__main__":
    app()
