"""``cado`` — unified CLI for scraping, ingesting and serving CADO data."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .db import connect, ingest_companies, ingest_lobbyists
from .scrape.companies import CompanyScraper, ScrapeStats
from .scrape.lobbyists import LobbyistScraper
from .settings import settings
from .storage import HtmlCache

app = typer.Typer(
    name="cado",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="markdown",
    help="Scrape, store, and serve Newfoundland's CADO public registry data.",
)
scrape_app = typer.Typer(no_args_is_help=True, help="Bulk-scrape the upstream site.")
ingest_app = typer.Typer(no_args_is_help=True, help="Re-parse cached HTML into DuckDB.")
app.add_typer(scrape_app, name="scrape")
app.add_typer(ingest_app, name="ingest")

console = Console()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, markup=False)],
    )
    # Quiet down the noisy libraries unless we're explicitly debugging.
    if not verbose:
        for noisy in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# scrape companies
# ---------------------------------------------------------------------------


@scrape_app.command("companies")
def scrape_companies_cmd(
    start: Annotated[int, typer.Option("--start", help="First company number (inclusive)")] = 1,
    stop: Annotated[int, typer.Option("--stop", help="Last company number (exclusive)")] = 105_000,
    concurrency: Annotated[int, typer.Option(help="Concurrent workers")] = settings.max_concurrency,
    rate: Annotated[
        float, typer.Option("--rate", help="Global requests/second cap")
    ] = settings.requests_per_second,
    rescrape: Annotated[
        bool, typer.Option("--rescrape", help="Re-fetch records already in the on-disk cache")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Enumerate company numbers from ``start`` to ``stop`` and cache each detail page."""
    _configure_logging(verbose)

    async def _run() -> None:
        stats = ScrapeStats()
        async with CompanyScraper(
            concurrency=concurrency,
            rate_per_second=rate,
            skip_cached=not rescrape,
        ) as scraper:
            with _progress(total=stop - start) as progress:
                task = progress.add_task("scraping companies", total=stop - start)
                async for outcome in scraper.scrape_numbers(range(start, stop)):
                    stats.record(outcome)
                    progress.update(
                        task,
                        advance=1,
                        description=(
                            f"scraping companies  "
                            f"[green]ok {stats.details}[/] "
                            f"[blue]list {stats.multi_hit_pages}[/] "
                            f"[yellow]empty {stats.empty}[/] "
                            f"[magenta]cached {stats.cached}[/] "
                            f"[red]err {stats.errors}[/]"
                        ),
                    )
        _print_company_stats(stats)

    asyncio.run(_run())


def _print_company_stats(stats: ScrapeStats) -> None:
    table = Table(title="Companies scrape complete", show_header=False)
    table.add_column("metric", style="cyan")
    table.add_column("count", justify="right", style="bold")
    table.add_row("attempted", str(stats.attempted))
    table.add_row("singletons (302 -> detail)", str(stats.details))
    table.add_row("multi-row list pages", str(stats.multi_hit_pages))
    table.add_row("suffixed records drilled", str(stats.suffixed_drilled))
    table.add_row("empty", str(stats.empty))
    table.add_row("cached (skipped)", str(stats.cached))
    table.add_row("errors", str(stats.errors))
    console.print(table)


# ---------------------------------------------------------------------------
# scrape lobbyists
# ---------------------------------------------------------------------------


@scrape_app.command("lobbyists")
def scrape_lobbyists_cmd(
    rate: Annotated[
        float, typer.Option("--rate", help="Global requests/second cap")
    ] = settings.requests_per_second,
    rescrape: Annotated[
        bool, typer.Option("--rescrape", help="Re-fetch records already in the on-disk cache")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Dump every registration in the lobbyist registry to the on-disk cache."""
    _configure_logging(verbose)

    async def _run() -> None:
        async with LobbyistScraper(rate_per_second=rate, skip_cached=not rescrape) as scraper:
            console.print("[cyan]Building index across all pages…[/]")
            total, entries = await scraper.build_index()
            console.print(
                f"Index built: [bold]{len(entries)}[/] entries (upstream says total = {total})."
            )

            details = 0
            skipped = 0
            errors = 0
            with _progress(total=len(entries)) as progress:
                task = progress.add_task("fetching lobbyist details", total=len(entries))
                async for outcome in scraper.scrape_details(entries):
                    if outcome.kind == "detail":
                        details += 1
                    elif outcome.kind == "skipped":
                        skipped += 1
                    else:
                        errors += 1
                    progress.update(
                        task,
                        advance=1,
                        description=(
                            f"fetching lobbyist details  "
                            f"[green]ok {details}[/] "
                            f"[magenta]skipped {skipped}[/] "
                            f"[red]err {errors}[/]"
                        ),
                    )
            console.print(
                f"Done. Saved [bold green]{details}[/] new, "
                f"skipped [magenta]{skipped}[/] already-cached, "
                f"[red]{errors}[/] errors."
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@ingest_app.command("companies")
def ingest_companies_cmd(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Re-parse cached company HTML into DuckDB."""
    _configure_logging(verbose)
    cache = HtmlCache(registry="companies")
    conn = connect()
    n_ok = n_err = 0
    keys = list(cache.iter_keys(kind="detail"))
    with _progress(total=len(keys)) as progress:
        task = progress.add_task("ingesting companies", total=len(keys))
        for result in ingest_companies(conn, cache):
            if result.parsed_ok:
                n_ok += 1
            else:
                n_err += 1
                if verbose:
                    console.print(f"  [red]error on {result.key}: {result.error}[/]")
            progress.update(
                task,
                advance=1,
                description=f"ingesting companies  [green]ok {n_ok}[/] [red]err {n_err}[/]",
            )
    console.print(f"Ingested [bold green]{n_ok}[/] companies, [red]{n_err}[/] errors.")
    conn.close()


@ingest_app.command("lobbyists")
def ingest_lobbyists_cmd(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Re-parse cached lobbyist HTML into DuckDB."""
    _configure_logging(verbose)
    cache = HtmlCache(registry="lobbyists")
    conn = connect()
    n_ok = n_err = 0
    keys = list(cache.iter_keys(kind="detail"))
    with _progress(total=len(keys)) as progress:
        task = progress.add_task("ingesting lobbyists", total=len(keys))
        for result in ingest_lobbyists(conn, cache):
            if result.parsed_ok:
                n_ok += 1
            else:
                n_err += 1
                if verbose:
                    console.print(f"  [red]error on {result.key}: {result.error}[/]")
            progress.update(
                task,
                advance=1,
                description=f"ingesting lobbyists  [green]ok {n_ok}[/] [red]err {n_err}[/]",
            )
    console.print(f"Ingested [bold green]{n_ok}[/] lobbyists, [red]{n_err}[/] errors.")
    conn.close()


@ingest_app.command("all")
def ingest_all_cmd(
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Run both ingesters."""
    ingest_companies_cmd(verbose=verbose)
    ingest_lobbyists_cmd(verbose=verbose)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on code changes (dev)")] = False,
) -> None:
    """Serve the HTMX search UI."""
    import uvicorn  # noqa: PLC0415 - deferred so non-serve commands don't pay the uvicorn import cost

    # ``cado.api:create_app`` is a factory; uvicorn calls it.
    uvicorn.run(
        "cado.api:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@app.command()
def info() -> None:
    """Print a summary of the on-disk cache and DuckDB contents."""
    table = Table(title="cado: on-disk state", show_header=True)
    table.add_column("location", style="cyan")
    table.add_column("count", justify="right", style="bold")
    table.add_column("details", style="dim")

    company_cache = HtmlCache(registry="companies")
    lobby_cache = HtmlCache(registry="lobbyists")
    table.add_row(
        str(
            company_cache.root.relative_to(settings.data_dir.parent)
            if settings.data_dir.parent in company_cache.root.parents
            else company_cache.root
        ),
        str(_count(company_cache.iter_keys(kind="detail"))),
        f"+ {_count(company_cache.iter_keys(kind='list'))} list pages",
    )
    table.add_row(
        str(
            lobby_cache.root.relative_to(settings.data_dir.parent)
            if settings.data_dir.parent in lobby_cache.root.parents
            else lobby_cache.root
        ),
        str(_count(lobby_cache.iter_keys(kind="detail"))),
        "",
    )

    if settings.duckdb_path.exists():
        conn = connect(settings.duckdb_path, read_only=True)
        for label, sql in [
            ("DuckDB: companies", "SELECT COUNT(*) FROM companies"),
            ("DuckDB: company_directors", "SELECT COUNT(*) FROM company_directors"),
            ("DuckDB: company_previous_names", "SELECT COUNT(*) FROM company_previous_names"),
            ("DuckDB: lobbyist_registrations", "SELECT COUNT(*) FROM lobbyist_registrations"),
            ("DuckDB: ingest_log", "SELECT COUNT(*) FROM ingest_log"),
        ]:
            n = conn.execute(sql).fetchone()[0]
            table.add_row(label, str(n), "")
        conn.close()
    else:
        table.add_row("DuckDB", "—", f"({settings.duckdb_path} does not exist yet)")

    console.print(table)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _progress(*, total: int) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _count(it: object) -> int:
    return sum(1 for _ in it)  # type: ignore[arg-type]


def main() -> None:
    """Entry-point referenced by ``[project.scripts]``."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
