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


@scrape_app.command("retry-errors")
def scrape_retry_errors_cmd(
    concurrency: Annotated[int, typer.Option(help="Concurrent workers")] = settings.max_concurrency,
    rate: Annotated[
        float, typer.Option("--rate", help="Global requests/second cap")
    ] = settings.requests_per_second,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Re-scrape only the numbers that errored in a previous run.

    Reads the deduplicated list of failed company numbers from
    ``data/scrape_errors_companies.jsonl``, truncates that file, then runs
    the scraper against just those numbers. Numbers that still fail are
    re-appended to the log for the next pass.
    """
    _configure_logging(verbose)

    numbers = CompanyScraper.read_error_log()
    if not numbers:
        console.print("[yellow]No errors logged from a previous run; nothing to retry.[/]")
        return

    console.print(f"Retrying [bold]{len(numbers)}[/] errored numbers.")
    error_log = settings.data_dir / "scrape_errors_companies.jsonl"
    # Truncate so this run produces a fresh log of just the *still*-failed
    # records. The scraper appends as it goes.
    if error_log.exists():
        error_log.unlink()

    async def _run() -> None:
        stats = ScrapeStats()
        async with CompanyScraper(
            concurrency=concurrency,
            rate_per_second=rate,
            # Force re-fetch: cached records would just re-fail the same way.
            skip_cached=False,
        ) as scraper:
            with _progress(total=len(numbers)) as progress:
                task = progress.add_task("retrying errors", total=len(numbers))
                async for outcome in scraper.scrape_numbers(numbers):
                    stats.record(outcome)
                    progress.update(
                        task,
                        advance=1,
                        description=(
                            f"retrying errors  "
                            f"[green]ok {stats.details}[/] "
                            f"[blue]list {stats.multi_hit_pages}[/] "
                            f"[yellow]empty {stats.empty}[/] "
                            f"[red]err {stats.errors}[/]"
                        ),
                    )
        _print_company_stats(stats)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# scrape lobbyists
# ---------------------------------------------------------------------------


@scrape_app.command("lobbyists")
def scrape_lobbyists_cmd(
    concurrency: Annotated[
        int, typer.Option(help="Concurrent detail-fetch workers")
    ] = settings.max_concurrency,
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
        async with LobbyistScraper(
            rate_per_second=rate,
            concurrency=concurrency,
            skip_cached=not rescrape,
        ) as scraper:
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
    reload: Annotated[
        bool, typer.Option("--reload", help="Auto-reload on code changes (dev)")
    ] = False,
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
# clean
# ---------------------------------------------------------------------------


clean_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Delete on-disk artifacts. Has three scopes:\n\n"
        "- `db`    — drop just the DuckDB (rebuildable via `cado ingest all`)\n"
        "- `cache` — drop the raw HTML cache (requires re-scraping to recover)\n"
        "- `all`   — drop both"
    ),
)
app.add_typer(clean_app, name="clean")


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return typer.confirm(prompt, default=False)


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"


def _dir_summary(path: object) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` under ``path`` (recursive)."""
    import contextlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    p = Path(str(path))
    if not p.exists():
        return 0, 0
    files = 0
    size = 0
    for entry in p.rglob("*"):
        if entry.is_file():
            files += 1
            with contextlib.suppress(OSError):
                size += entry.stat().st_size
    return files, size


def _delete_duckdb() -> int:
    """Remove ``cado.duckdb`` and its sidecar WAL file. Returns bytes freed."""
    import contextlib  # noqa: PLC0415

    freed = 0
    for candidate in (
        settings.duckdb_path,
        settings.duckdb_path.with_suffix(".duckdb.wal"),
    ):
        if candidate.exists():
            with contextlib.suppress(OSError):
                freed += candidate.stat().st_size
            candidate.unlink()
    return freed


def _delete_cache() -> tuple[int, int]:
    """Remove the entire HTML cache directory tree. Returns (files, bytes) freed."""
    import shutil  # noqa: PLC0415

    files, size = _dir_summary(settings.html_cache_dir)
    if settings.html_cache_dir.exists():
        shutil.rmtree(settings.html_cache_dir)
    return files, size


@clean_app.command("db")
def clean_db_cmd(
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Delete the DuckDB file. The on-disk HTML cache is untouched.

    After this you can rebuild the database from the cache with
    `cado ingest all` — no upstream traffic required.
    """
    if not settings.duckdb_path.exists():
        console.print(f"[yellow]Nothing to do: {settings.duckdb_path} does not exist.[/]")
        return

    size = settings.duckdb_path.stat().st_size
    console.print(f"About to delete [bold]{settings.duckdb_path}[/] ({_human_bytes(size)}).")
    console.print("[dim]The HTML cache is kept; `cado ingest all` will rebuild the database.[/]")
    if not _confirm("Continue?", assume_yes=yes):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    freed = _delete_duckdb()
    console.print(f"Deleted DuckDB. [green]{_human_bytes(freed)} freed.[/]")


@clean_app.command("cache")
def clean_cache_cmd(
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Delete the raw HTML cache. The DuckDB is untouched.

    This is the destructive option: scraped HTML is the only source of truth
    for re-parsing. After this you must re-scrape to recover.
    """
    n, size = _dir_summary(settings.html_cache_dir)
    if n == 0:
        console.print(f"[yellow]Nothing to do: {settings.html_cache_dir} is empty or missing.[/]")
        return

    console.print(
        f"About to delete [bold]{settings.html_cache_dir}[/] ({n} files, {_human_bytes(size)})."
    )
    console.print(
        "[red bold]This is destructive![/] The raw HTML cache is the only "
        "source of truth — you'll have to re-scrape to recover."
    )
    if not _confirm("Continue?", assume_yes=yes):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    files, freed = _delete_cache()
    console.print(f"Deleted {files} files. [green]{_human_bytes(freed)} freed.[/]")


@clean_app.command("all")
def clean_all_cmd(
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Delete the DuckDB *and* the HTML cache. Wipes all on-disk data."""
    n_files, cache_size = _dir_summary(settings.html_cache_dir)
    db_size = settings.duckdb_path.stat().st_size if settings.duckdb_path.exists() else 0
    total = cache_size + db_size

    if n_files == 0 and db_size == 0:
        console.print("[yellow]Nothing to do: no data on disk.[/]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("location")
    table.add_column("files", justify="right")
    table.add_column("size", justify="right")
    if n_files:
        table.add_row(str(settings.html_cache_dir), str(n_files), _human_bytes(cache_size))
    if db_size:
        table.add_row(str(settings.duckdb_path), "1", _human_bytes(db_size))
    table.add_row("", "", "")
    table.add_row("[bold]total[/]", str(n_files + (1 if db_size else 0)), _human_bytes(total))
    console.print(table)

    console.print("[red bold]About to wipe all CADO data on disk.[/] Re-scraping will take hours.")
    if not _confirm("Continue?", assume_yes=yes):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    freed_db = _delete_duckdb()
    _, freed_cache = _delete_cache()
    console.print(f"Deleted everything. [green]{_human_bytes(freed_db + freed_cache)} freed.[/]")


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
