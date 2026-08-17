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
from .extract.linker import _NAME_FRAGMENTS  # noqa: PLC2701 - shared list, single source
from .logging import configure_logging
from .pipeline import extract_page, run_pipeline_locked
from .storage import Storage

app = typer.Typer(
    add_completion=False,
    help="Collection and extraction pipeline for ransomware leak-site monitoring.",
)
sources_app = typer.Typer(help="Manage monitored sources.")
app.add_typer(sources_app, name="sources")
mirrors_app = typer.Typer(help="Onion addresses discovered on crawled pages.")
app.add_typer(mirrors_app, name="mirrors")

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


# ---------------------------------------------------------------- mirrors


@mirrors_app.command("list")
def mirrors_list(
    slug: str | None = typer.Argument(None, help="Limit to one source"),
) -> None:
    """Onion addresses seen on crawled pages.

    These are recorded, not followed. A leak site announcing "our new address is X" is
    text written by the site being crawled, so promoting one is a decision you make, not
    something the crawler does because a page said so.
    """
    _setup()

    async def work(storage: Storage, _: object) -> list:  # type: ignore[type-arg]
        return await storage.list_mirrors(slug)

    rows = asyncio.run(_with_storage(work))
    if not rows:
        typer.echo("No mirror addresses recorded yet.")
        return

    typer.echo(f"{'SOURCE':<16} {'STATUS':<14} {'SEEN':<6} ADDRESS")
    for row in rows:
        typer.echo(
            f"{row['slug']:<16} {row['status']:<14} {row['times_seen']:<6} {row['onion_host']}"
        )


@mirrors_app.command("approve")
def mirrors_approve(
    slug: str = typer.Argument(..., help="Source slug"),
    onion_host: str = typer.Argument(..., help="The .onion host to approve"),
) -> None:
    """Mark an address as trusted, so failover will prefer it."""
    _setup()

    async def work(storage: Storage, _: object) -> int:
        return await storage.set_mirror_status(slug, onion_host.lower(), "approved")

    count = asyncio.run(_with_storage(work))
    typer.echo(f"Approved {count} address(es) for {slug}.")


@mirrors_app.command("reject")
def mirrors_reject(
    slug: str = typer.Argument(..., help="Source slug"),
    onion_host: str = typer.Argument(..., help="The .onion host to reject"),
) -> None:
    """Mark an address as untrusted. Rejection sticks, however often the address reappears."""
    _setup()

    async def work(storage: Storage, _: object) -> int:
        return await storage.set_mirror_status(slug, onion_host.lower(), "rejected")

    count = asyncio.run(_with_storage(work))
    typer.echo(f"Rejected {count} address(es) for {slug}.")


@mirrors_app.command("use")
def mirrors_use(
    slug: str = typer.Argument(..., help="Source slug"),
    url: str = typer.Argument(..., help="Full URL to crawl this source at from now on"),
) -> None:
    """Point a source at a different address.

    Written to `sources.active_url`, so `intel sources sync` will not undo it — unlike
    editing `base_url`, which the file overwrites on every sync.
    """
    _setup()

    async def work(storage: Storage, _: object) -> bool:
        source = await storage.get_source(slug)
        if source is None:
            return False
        await storage.promote_mirror(source.id, url)
        return True

    ok = asyncio.run(_with_storage(work))
    if not ok:
        typer.echo(f"No source {slug!r}.", err=True)
        raise typer.Exit(1)
    typer.echo(f"{slug} will now be crawled at {url}")


@mirrors_app.command("reset")
def mirrors_reset(slug: str = typer.Argument(..., help="Source slug")) -> None:
    """Go back to the address in sources.yaml."""
    _setup()

    async def work(storage: Storage, _: object) -> int:
        return await storage.clear_active_url(slug)

    count = asyncio.run(_with_storage(work))
    typer.echo(f"Reset {count} source(s) to their configured address.")


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

    Refuses to start while another crawl is in flight — including the worker's hourly
    scheduled run. Two crawls through one Tor daemon compete for circuits and both get
    slower, which shows up as sources failing with "TTL expired" that work fine alone.
    """
    _setup()

    async def work(storage: Storage, settings: object):  # type: ignore[no-untyped-def]
        return await run_pipeline_locked(
            storage=storage,
            settings=settings,  # type: ignore[arg-type]
            slugs=list(source) if source else None,
            extractor_name=extractor,
        )

    result = asyncio.run(_with_storage(work))

    if result is None:
        typer.echo(
            "Another crawl is already running (the worker's hourly run, or a second "
            "`intel run`). Nothing was crawled.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(
        f"Done. {result.inserted} new leaks, {result.updated} seen again, "
        f"{len(result.failed)} source(s) failed."
    )
    switched = [s for s in result.sources if s.switched_to]
    for source_result in switched:
        typer.echo(f"Switched {source_result.slug} to mirror {source_result.switched_to}")
    discovered = sum(s.mirrors_found for s in result.sources)
    if discovered:
        typer.echo(f"{discovered} new onion address(es) recorded. See `intel mirrors list`.")
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
        upserted = await storage.upsert_leaks(leaks, source_id=row.id if row else None)
        # Any path that creates leaks evaluates alerts, or a leak loaded this way would be
        # the one thing a watching alert silently misses.
        events = await storage.match_alerts(upserted.new_leak_ids)
        return upserted, events

    result, events = asyncio.run(_with_storage(work))
    typer.echo(
        f"Loaded: {result.inserted} new, {result.updated} updated, {result.skipped} skipped."
    )
    if events:
        typer.echo(f"{events} alert event(s) created.")


@app.command("repair-domains")
def repair_domains(
    apply: bool = typer.Option(
        False, "--apply", help="Actually write the corrections (default is a dry run)"
    ),
) -> None:
    """Re-key leaks whose victim_domain came from the listing next to theirs.

    Sites that print the victim's link *above* the company name — termite, lockbit and
    eight others — used to have every listing on the page take the following listing's
    domain. `victim_domain` is half of `dedupe_hash`, so those rows are filed under another
    company's identity, and a domain alert would fire for the wrong company.

    The fix runs over `raw_pages`, which still holds the text of everything fetched, so
    history can be corrected without waiting for a re-crawl. Rows are corrected in place —
    `first_seen_at` is preserved, because "what is new since yesterday" is the one thing
    that cannot be reconstructed later.

    Dry run by default. Re-running after an apply is safe and reports nothing to do.
    """
    _setup()

    async def work(storage: Storage, _: object) -> dict[str, int]:
        tally = {"repaired": 0, "merged": 0, "missing": 0}

        for source_id, slug, text in await storage.latest_pages():
            del source_id
            for leak in extract_page(
                text, source_group=slug, source_url=None, page_no=1, extractor_name="rules"
            ):
                if not leak.victim_domain or not leak.victim_name:
                    continue

                if not apply:
                    exists = await storage._pool.fetchval(  # noqa: SLF001 - read-only probe
                        "select 1 from leaks where dedupe_hash = $1", leak.dedupe_hash
                    )
                    if not exists:
                        tally["repaired"] += 1
                    continue

                outcome = await storage.repair_victim_domain(
                    actor_group=leak.actor_group,
                    victim_name=leak.victim_name,
                    victim_domain=leak.victim_domain,
                    new_hash=leak.dedupe_hash,
                )
                tally[outcome] += 1

        if apply:
            # Same generation of extraction bug, same repair: rows labelled with a name
            # fragment ("Ltd", "Financial") that a re-crawl cannot clear on its own.
            tally["unnamed"] = await storage.clear_fragment_victim_names(
                sorted(_NAME_FRAGMENTS)
            )

        return tally

    tally = asyncio.run(_with_storage(work))

    if not apply:
        typer.echo(f"Dry run: {tally['repaired']} listing(s) would be re-keyed.")
        typer.echo("Pass --apply to write the corrections.")
        return

    typer.echo(
        f"Repaired {tally['repaired']}, merged {tally['merged']} duplicate(s), "
        f"{tally['missing']} already correct or not stored."
    )
    if tally.get("unnamed"):
        typer.echo(
            f"Cleared {tally['unnamed']} victim name(s) that were only a name fragment; "
            f"those rows are now identified by their domain."
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
