"""Operational tests for immutable snapshot build and publication."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from cado.refresh import run_refresh
from cado.snapshot import (
    SnapshotValidationError,
    build_snapshot,
    open_workspace,
    publish_snapshot,
    validate_database,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _ready_workspace(
    data_dir: Path,
    *,
    company_fixture: str = "c_50000_active_with_directors.html",
):
    workspace = open_workspace(data_dir, company_start=1, company_stop=105_000)
    workspace.company_cache().write(
        "company",
        (FIXTURES / "companies" / company_fixture).read_text(),
    )
    workspace.lobbyist_cache().write(
        "IHL-867-1005",
        (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text(),
    )
    workspace.manifest.company_fetch_complete = True
    workspace.manifest.lobbyist_fetch_complete = True
    workspace.manifest.lobbyist_expected_count = 1
    workspace.manifest.source_fetched_at = datetime.now(UTC)
    workspace.save()
    return workspace


def test_build_validate_and_publish_snapshot(tmp_path: Path) -> None:
    live = tmp_path / "cado.duckdb"
    workspace = _ready_workspace(tmp_path)

    report = build_snapshot(workspace, live_db_path=live)
    assert report.company_count == 1
    assert report.lobbyist_count == 1
    assert not live.exists()

    publish_snapshot(workspace, live_db_path=live)
    metadata = validate_database(live)
    assert metadata.snapshot_id == report.snapshot_id
    assert metadata.source_fetched_at is not None
    assert metadata.snapshot_built_at is not None
    assert metadata.published_at is not None
    assert (tmp_path / "snapshots" / report.snapshot_id / "html").is_dir()


def test_refresh_orchestrator_resumes_fetched_workspace_without_network(tmp_path: Path) -> None:
    workspace = _ready_workspace(tmp_path)
    snapshot_id = workspace.manifest.snapshot_id

    result = run_refresh(
        data_dir=tmp_path,
        live_db_path=tmp_path / "cado.duckdb",
        company_start=1,
        company_stop=105_000,
        discovery_tail=1_000,
        concurrency=1,
        rate=1,
        max_count_drop_fraction=0.2,
    )

    assert result.report.snapshot_id == snapshot_id
    assert result.company_stats is None
    assert validate_database(tmp_path / "cado.duckdb").snapshot_id == snapshot_id


def test_atomic_publish_keeps_open_readers_and_previous_snapshot(tmp_path: Path) -> None:
    live = tmp_path / "cado.duckdb"
    first = _ready_workspace(tmp_path)
    first_id = build_snapshot(first, live_db_path=live).snapshot_id
    publish_snapshot(first, live_db_path=live)
    old_reader = duckdb.connect(str(live), read_only=True)

    second = _ready_workspace(tmp_path, company_fixture="c_73498_condo.html")
    second_id = build_snapshot(second, live_db_path=live).snapshot_id
    publish_snapshot(second, live_db_path=live)

    assert old_reader.execute("SELECT number FROM companies").fetchone() == ("50000",)
    old_reader.close()
    new_reader = duckdb.connect(str(live), read_only=True)
    assert new_reader.execute("SELECT number FROM companies").fetchone() == ("73498",)
    new_reader.close()
    previous = tmp_path / "cado.previous.duckdb"
    assert validate_database(previous).snapshot_id == first_id
    assert validate_database(live).snapshot_id == second_id


def test_parse_failure_never_replaces_live_snapshot(tmp_path: Path) -> None:
    live = tmp_path / "cado.duckdb"
    first = _ready_workspace(tmp_path)
    first_id = build_snapshot(first, live_db_path=live).snapshot_id
    publish_snapshot(first, live_db_path=live)

    broken = _ready_workspace(tmp_path)
    broken.company_cache().write("broken", "<html>not a company</html>")
    with pytest.raises(SnapshotValidationError, match="parse error"):
        build_snapshot(broken, live_db_path=live)
    assert validate_database(live).snapshot_id == first_id
    assert not broken.candidate_db_path.exists()


def test_suspicious_count_drop_refuses_publication(tmp_path: Path) -> None:
    live = tmp_path / "cado.duckdb"
    first = _ready_workspace(tmp_path)
    for filename in (
        "c_73498_condo.html",
        "c_69963_coop_cancelled.html",
        "c_2D_extraprov_old.html",
        "c_99000_extraprov_active.html",
    ):
        first.company_cache().write(filename, (FIXTURES / "companies" / filename).read_text())
    first_id = build_snapshot(first, live_db_path=live).snapshot_id
    publish_snapshot(first, live_db_path=live)

    smaller = _ready_workspace(tmp_path)
    with pytest.raises(SnapshotValidationError, match="count dropped"):
        build_snapshot(smaller, live_db_path=live)
    assert validate_database(live).snapshot_id == first_id


def test_only_two_raw_snapshot_archives_are_retained(tmp_path: Path) -> None:
    live = tmp_path / "cado.duckdb"
    published_ids: list[str] = []
    for fixture in (
        "c_50000_active_with_directors.html",
        "c_73498_condo.html",
        "c_69963_coop_cancelled.html",
    ):
        workspace = _ready_workspace(tmp_path, company_fixture=fixture)
        published_ids.append(build_snapshot(workspace, live_db_path=live).snapshot_id)
        publish_snapshot(workspace, live_db_path=live)

    archives = {path.name for path in (tmp_path / "snapshots").iterdir()}
    assert archives == set(published_ids[-2:])
