"""`intel` — the command line for the collection pipeline.

Every operation is a command that can be scripted, scheduled, and re-run safely.
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


@sources_app.command("probe")
def sources_probe(
    url: str = typer.Argument(..., help="The page to test — the victim list itself"),
    collector: str = typer.Option(
        "auto", help="auto (http, then browser if http fails the checks) | http | browser"
    ),
    save: Path | None = typer.Option(
        None, help="Directory to write the fetched page's .html and .txt into"
    ),
) -> None:
    """Check a candidate site against the source checklist before adding it.

    Fetches page 1 over Tor exactly as a crawl would and reports: reachable, gated,
    readable, a victim listing (and not a forum or shop), which collector it needs, how it
    paginates, and any new address it announces. Writes nothing to the database.

        intel sources probe http://example....onion/leaks
    """
    from .collectors import get_collector  # noqa: PLC0415 - keep CLI start-up light
    from .probe import evaluate, yaml_snippet  # noqa: PLC0415

    _setup()
    settings = get_settings()
    kinds = ["http", "browser"] if collector == "auto" else [collector]

    async def fetch(kind: str):  # type: ignore[no-untyped-def]
        client = get_collector(
            kind,
            host=settings.tor_host,
            socks_ports=settings.tor_socks_ports,
            timeout=settings.request_timeout_seconds,
            max_retries=2,
            backoff_seconds=settings.retry_backoff_seconds,
            backoff_cap_seconds=settings.retry_backoff_cap_seconds,
        )
        try:
            # A hard ceiling on top of the collector's own timeouts: a hidden service that
            # accepts the connection and then trickles bytes can otherwise hold this forever.
            html = await asyncio.wait_for(
                client.fetch(url), settings.request_timeout_seconds * 3
            )
            return html, client.last_error
        except TimeoutError:
            return None, "no complete response (connection hung)"
        except ImportError as exc:
            return None, str(exc).splitlines()[0]
        finally:
            await client.aclose()

    passed = None
    for kind in kinds:
        typer.echo(f"\n== {kind} ==  fetching {url} (a Tor fetch takes up to a minute)")
        html, error = asyncio.run(fetch(kind))
        report = evaluate(url, kind, html, error)

        if save is not None and html is not None:
            save.mkdir(parents=True, exist_ok=True)
            (save / f"probe.{kind}.html").write_text(html, encoding="utf-8")
            (save / f"probe.{kind}.txt").write_text(report.text, encoding="utf-8")

        if report.reachable:
            typer.echo(
                f"   fetched {report.html_bytes:,} bytes of HTML -> "
                f"{len(report.text.strip()):,} chars of text"
            )
            first = report.text.strip().replace("\n", " | ")[:200]
            typer.echo(f"   page starts: {first}")
        typer.echo(f"   victims extracted: {len(report.victims)}")
        for leak in report.victims[:10]:
            typer.echo(f"     - {leak.victim_name or '(no name)':<40} {leak.victim_domain or ''}")
        typer.echo(f"   pagination: {report.pagination}")

        problems = report.problems()
        for problem in problems:
            typer.echo(f"   FAIL  {problem}")
        if report.announced_mirrors:
            typer.echo("   The page announces other addresses for itself — probe these too:")
            for mirror in report.announced_mirrors:
                typer.echo(f"     {mirror}")
        if report.other_onions:
            typer.echo("   Other onion addresses on the page (a move notice may be among them):")
            for other in report.other_onions[:10]:
                typer.echo(f"     {other}")
        if report.listing_links and not report.usable and not report.off_topic_site:
            typer.echo("   Links on this page that may be the actual listing — probe these:")
            for link in report.listing_links:
                typer.echo(f"     {link}")

        if report.usable:
            typer.echo(f"   PASS  a readable leak listing with collector: {kind}")
            passed = report
            break
        if report.off_topic_site:
            # Rendering the page in a browser does not turn a forum into a leak site.
            break

    typer.echo("")
    if passed is None:
        typer.echo("Verdict: do not add this URL as it is. Fix what FAIL says above first.")
        raise typer.Exit(1)
    typer.echo(
        "Verdict: good to add. Read the victim names above — if they are thread titles, "
        "products or menu items, it is not a leak site whatever the count says.\n"
    )
    typer.echo(yaml_snippet(url, passed.collector, passed.pagination))


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
    extractor: str | None = typer.Option(None, help="rules"),
    due_only: bool = typer.Option(
        False,
        "--due-only",
        help="Skip sources whose crawl_interval_seconds has not elapsed (what the scheduler does)",
    ),
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
            only_due=due_only,
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
    if result.discarded:
        # Pages a doubling wave fetched past the end of a listing. A large number here means
        # CRAWL_PAGE_CONCURRENCY is tuned well above how deep these sources actually go.
        typer.echo(
            f"{result.discarded} page(s) fetched past the end of a listing and discarded. "
            f"Lower CRAWL_PAGE_CONCURRENCY if that number is large."
        )
    if result.failed:
        typer.echo(f"Failed: {', '.join(result.failed)}", err=True)


@app.command("extract-file")
def extract_file(
    path: Path = typer.Argument(..., help="A text file to extract from"),
    group: str = typer.Option("unknown", help="Ransomware group slug for these listings"),
    extractor: str = typer.Option("rules", help="rules"),
    load: bool = typer.Option(False, "--load", help="Write results to the database"),
) -> None:
    """Extract leaks from a local text file.

    This is the offline path for testing an extractor against saved page text without
    touching Tor.
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


@app.command("repair-domains")
def repair_domains(
    apply: bool = typer.Option(
        False, "--apply", help="Actually write the corrections (default is a dry run)"
    ),
) -> None:
    """Re-key leaks whose victim_domain came from the listing next to theirs.

    On sites that print the victim's link *above* the company name — termite, lockbit and
    eight others — an extraction that attaches links by reading order gives every listing
    the following listing's domain. `victim_domain` is half of `dedupe_hash`, so such rows
    are filed under another company's identity.

    The repair runs over `raw_pages`, which holds the text of everything fetched, so stored
    rows can be corrected without waiting for a re-crawl. Rows are corrected in place —
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


@app.command("backfill-descriptions")
def backfill_descriptions(
    apply: bool = typer.Option(
        False, "--apply", help="Actually write the summaries (default is a dry run)"
    ),
) -> None:
    """Fill in summaries and incident types for leaks collected before they existed.

    Runs the current extractor over every page in `raw_pages`, oldest first, and writes each
    listing's description and types onto the leak it identifies. Oldest first so that when a
    listing appears on several pages over time, its most recent wording is the one kept.

    Only existing leaks are touched — nothing is inserted, and `first_seen_at` is never
    written. Dry run by default; re-running after an apply is safe.
    """
    _setup()

    async def work(storage: Storage, _: object) -> dict[str, int]:
        tally = {"pages": 0, "listings": 0, "described": 0, "updated": 0}
        async for slug, url, page_no, text in storage.iter_raw_pages():
            tally["pages"] += 1
            leaks = extract_page(
                text, source_group=slug, source_url=url, page_no=page_no, extractor_name="rules"
            )
            tally["listings"] += len(leaks)
            tally["described"] += sum(1 for leak in leaks if leak.summary)
            if apply:
                tally["updated"] += await storage.apply_descriptions(
                    [(leak.dedupe_hash, leak.summary, leak.incident_types) for leak in leaks]
                )
        return tally

    tally = asyncio.run(_with_storage(work))
    typer.echo(
        f"Read {tally['pages']} page(s): {tally['listings']} listing(s), "
        f"{tally['described']} with a description."
    )
    if not apply:
        typer.echo("Dry run. Pass --apply to write them.")
        return
    typer.echo(f"Wrote {tally['updated']} update(s) onto stored leaks.")


@app.command("feeds")
def feeds(
    only: str = typer.Option(
        "all",
        "--only",
        help="Which feeds: all | iocs (URLhaus, ThreatFox, TweetFeed) | ransomware "
        "(ransomware.live) | mobile (scam phone-number reports)",
    ),
) -> None:
    """Fetch the public feeds once, now, rather than waiting for the worker's schedule.

    Runs the same jobs the worker runs on its crons, so what lands is exactly what the next
    scheduled run would have written. Safe to repeat: every feed upserts.
    """
    _setup()
    # Imported here: the tasks module builds the worker's settings at import time, which
    # every other command has no need for.
    from . import tasks

    jobs = {
        "iocs": tasks.fetch_feeds,
        "ransomware": tasks.fetch_ransomware_feed,
        "mobile": tasks.fetch_mobile_reports,
    }
    if only != "all" and only not in jobs:
        raise typer.BadParameter(f"expected all, {', '.join(jobs)}; got {only!r}")
    selected = jobs if only == "all" else {only: jobs[only]}

    async def work(storage: Storage, settings: object) -> dict[str, object]:
        ctx = {"storage": storage, "settings": settings}
        return {name: await job(ctx) for name, job in selected.items()}

    for name, outcome in asyncio.run(_with_storage(work)).items():
        typer.echo(f"{name}: {outcome}")


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
