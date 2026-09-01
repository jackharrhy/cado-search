"""Shared pytest fixtures."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cado.db import connect, ingest_one_html

FIXTURES = Path(__file__).parent / "fixtures"
COMPANIES = FIXTURES / "companies"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Build a small DuckDB shared by UI, query, and MCP contract tests."""
    db_path = tmp_path / "cado.duckdb"
    conn = connect(db_path)
    for key, filename in [
        ("50000", "c_50000_active_with_directors.html"),
        ("73498", "c_73498_condo.html"),
        ("69963", "c_69963_coop_cancelled.html"),
        ("2D", "c_2D_extraprov_old.html"),
        ("99000", "c_99000_extraprov_active.html"),
    ]:
        html = (COMPANIES / filename).read_text(encoding="utf-8")
        ingest_one_html(conn, "company", key, html)
    lobbyist_html = (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text(encoding="utf-8")
    ingest_one_html(conn, "lobbyist", "IHL-867-1005", lobbyist_html)
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO snapshot_metadata (
            singleton, schema_version, snapshot_id, fetch_started_at,
            source_fetched_at, snapshot_built_at, published_at,
            company_start, company_stop, lobbyist_expected_count,
            company_cache_count, lobbyist_cache_count
        ) VALUES (TRUE, 2, 'test-snapshot', ?, ?, ?, ?, 1, 105000, 1, 5, 1)
        """,
        [now, now, now, now],
    )
    conn.close()
    return db_path


# Network-touching tests are opt-in via CADO_LIVE_TESTS=1 so CI / casual `pytest`
# runs never hit the public site.
live_required = pytest.mark.skipif(
    os.environ.get("CADO_LIVE_TESTS") != "1",
    reason="set CADO_LIVE_TESTS=1 to run tests that hit cado.eservices.gov.nl.ca",
)
