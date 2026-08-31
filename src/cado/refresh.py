"""Orchestrate one resumable full-registry refresh."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .scrape.companies import CompanyScraper, ScrapeStats
from .scrape.lobbyists import LobbyistScraper
from .snapshot import (
    SnapshotError,
    SnapshotManifest,
    SnapshotReport,
    SnapshotWorkspace,
    build_snapshot,
    open_workspace,
    publish_snapshot,
    refresh_lock,
)

CompanyProgress = Callable[[ScrapeStats], None]
LobbyistProgress = Callable[[int, int, int], None]
WorkspaceProgress = Callable[[SnapshotManifest], None]


@dataclass(slots=True, frozen=True)
class RefreshResult:
    report: SnapshotReport
    published_at: datetime
    company_stats: ScrapeStats | None


def run_refresh(
    *,
    data_dir: Path,
    live_db_path: Path,
    company_start: int,
    company_stop: int,
    discovery_tail: int,
    concurrency: int,
    rate: float,
    max_count_drop_fraction: float,
    on_workspace: WorkspaceProgress | None = None,
    on_company: CompanyProgress | None = None,
    on_lobbyist: LobbyistProgress | None = None,
) -> RefreshResult:
    """Fetch both registries and publish only a complete validated snapshot."""
    if discovery_tail < 1 or discovery_tail >= company_stop - company_start:
        raise ValueError("discovery-tail must be positive and smaller than the range")
    with refresh_lock(data_dir):
        workspace = open_workspace(
            data_dir,
            company_start=company_start,
            company_stop=company_stop,
        )
        if on_workspace:
            on_workspace(workspace.manifest)
        company_stats = _ensure_company_fetch(
            workspace,
            discovery_tail=discovery_tail,
            concurrency=concurrency,
            rate=rate,
            on_progress=on_company,
        )
        _ensure_lobbyist_fetch(
            workspace,
            concurrency=concurrency,
            rate=rate,
            on_progress=on_lobbyist,
        )
        if workspace.manifest.source_fetched_at is None:
            workspace.manifest.source_fetched_at = datetime.now(UTC)
        workspace.save()
        report = build_snapshot(
            workspace,
            live_db_path=live_db_path,
            max_count_drop_fraction=max_count_drop_fraction,
        )
        published_at = publish_snapshot(workspace, live_db_path=live_db_path)
    return RefreshResult(report, published_at, company_stats)


def _ensure_company_fetch(
    workspace: SnapshotWorkspace,
    *,
    discovery_tail: int,
    concurrency: int,
    rate: float,
    on_progress: CompanyProgress | None,
) -> ScrapeStats | None:
    if workspace.manifest.company_fetch_complete:
        return None
    stats = asyncio.run(
        _fetch_companies(
            workspace,
            concurrency=concurrency,
            rate=rate,
            on_progress=on_progress,
        )
    )
    if stats.errors:
        raise SnapshotError(
            f"company fetch has {stats.errors} error(s); rerun the same command to retry"
        )
    cache = workspace.company_cache()
    stop = workspace.manifest.company_stop
    tail_is_empty = all(
        cache.completed_outcome(str(number)) == "empty"
        for number in range(stop - discovery_tail, stop)
    )
    if not tail_is_empty:
        raise SnapshotError(
            f"company range ends without {discovery_tail} consecutive empty seeds; "
            f"rerun with --stop greater than {stop}"
        )
    workspace.manifest.company_fetch_complete = True
    workspace.save()
    return stats


def _ensure_lobbyist_fetch(
    workspace: SnapshotWorkspace,
    *,
    concurrency: int,
    rate: float,
    on_progress: LobbyistProgress | None,
) -> None:
    if workspace.manifest.lobbyist_fetch_complete:
        return
    total, count, errors = asyncio.run(
        _fetch_lobbyists(
            workspace,
            concurrency=concurrency,
            rate=rate,
            on_progress=on_progress,
        )
    )
    if total is None or count != total:
        raise SnapshotError(
            f"lobbyist index is incomplete: parsed {count}, upstream reported {total}"
        )
    if errors:
        raise SnapshotError(f"lobbyist fetch has {errors} error(s); rerun to retry missing details")
    workspace.manifest.lobbyist_expected_count = total
    workspace.manifest.lobbyist_fetch_complete = True
    workspace.save()


async def _fetch_companies(
    workspace: SnapshotWorkspace,
    *,
    concurrency: int,
    rate: float,
    on_progress: CompanyProgress | None,
) -> ScrapeStats:
    stats = ScrapeStats()
    manifest = workspace.manifest
    async with CompanyScraper(
        cache=workspace.company_cache(),
        concurrency=concurrency,
        rate_per_second=rate,
        skip_cached=True,
        error_log_path=workspace.root / "company-errors.jsonl",
    ) as scraper:
        async for outcome in scraper.scrape_range(
            manifest.company_start,
            manifest.company_stop,
        ):
            stats.record(outcome)
            if on_progress:
                on_progress(stats)
    return stats


async def _fetch_lobbyists(
    workspace: SnapshotWorkspace,
    *,
    concurrency: int,
    rate: float,
    on_progress: LobbyistProgress | None,
) -> tuple[int | None, int, int]:
    async with LobbyistScraper(
        cache=workspace.lobbyist_cache(),
        rate_per_second=rate,
        concurrency=concurrency,
        skip_cached=True,
    ) as scraper:
        total, entries = await scraper.build_index()
        if total is None or len(entries) != total:
            return total, len(entries), 0
        errors = 0
        completed = 0
        async for outcome in scraper.scrape_details(entries):
            completed += 1
            errors += outcome.kind == "error"
            if on_progress:
                on_progress(completed, len(entries), errors)
    return total, len(entries), errors
