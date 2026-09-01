"""Tests for the single-container process supervisor."""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from cado.container import _seconds_until_due, supervise


def _python(source: str, *args: Path) -> tuple[str, ...]:
    return (sys.executable, "-c", source, *(str(arg) for arg in args))


@pytest.mark.asyncio
async def test_missing_snapshot_is_built_before_server_starts(
    tmp_path: Path,
    seeded_db: Path,
) -> None:
    db_path = tmp_path / "data" / "cado.duckdb"
    events = tmp_path / "events"
    refresh = _python(
        "import shutil, sys; from pathlib import Path; "
        "Path(sys.argv[2]).parent.mkdir(); shutil.copyfile(sys.argv[1], sys.argv[2]); "
        "Path(sys.argv[3]).write_text('refresh\\n')",
        seeded_db,
        db_path,
        events,
    )
    server = _python(
        "import sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.write_text(p.read_text() + 'server\\n')",
        events,
    )

    result = await supervise(
        db_path=db_path,
        server_command=server,
        refresh_command=refresh,
        refresh_interval=timedelta(days=30),
        retry_interval=timedelta(days=1),
    )

    assert result == 0
    assert events.read_text().splitlines() == ["refresh", "server"]


@pytest.mark.asyncio
async def test_invalid_snapshot_is_kept_before_bootstrap(
    tmp_path: Path,
    seeded_db: Path,
) -> None:
    db_path = tmp_path / "cado.duckdb"
    good_snapshot = tmp_path / "good.duckdb"
    shutil.copyfile(seeded_db, good_snapshot)
    db_path.write_text("broken")
    refresh = _python(
        "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])",
        good_snapshot,
        db_path,
    )
    server = _python("pass")

    result = await supervise(
        db_path=db_path,
        server_command=server,
        refresh_command=refresh,
        refresh_interval=timedelta(days=30),
        retry_interval=timedelta(days=1),
    )

    assert result == 0
    assert (tmp_path / "cado.invalid.duckdb").read_text() == "broken"


@pytest.mark.asyncio
async def test_due_snapshot_refreshes_while_server_keeps_running(
    tmp_path: Path,
    seeded_db: Path,
) -> None:
    conn = duckdb.connect(str(seeded_db))
    conn.execute(
        "UPDATE snapshot_metadata SET source_fetched_at = ?",
        [datetime.now(UTC) - timedelta(days=2)],
    )
    conn.close()
    refreshed = tmp_path / "refreshed"
    refresh = _python(
        "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('yes')",
        refreshed,
    )
    server = _python("import sys, time; time.sleep(0.2); sys.exit(7)")

    result = await supervise(
        db_path=seeded_db,
        server_command=server,
        refresh_command=refresh,
        refresh_interval=timedelta(days=1),
        retry_interval=timedelta(days=1),
    )

    assert result == 7
    assert refreshed.read_text() == "yes"


@pytest.mark.asyncio
async def test_failed_scheduled_refresh_does_not_stop_server(seeded_db: Path) -> None:
    conn = duckdb.connect(str(seeded_db))
    conn.execute(
        "UPDATE snapshot_metadata SET source_fetched_at = ?",
        [datetime.now(UTC) - timedelta(days=2)],
    )
    conn.close()

    result = await supervise(
        db_path=seeded_db,
        server_command=_python("import sys, time; time.sleep(0.2); sys.exit(7)"),
        refresh_command=_python("import sys; sys.exit(9)"),
        refresh_interval=timedelta(days=1),
        retry_interval=timedelta(days=1),
    )

    assert result == 7


@pytest.mark.asyncio
async def test_failed_initial_refresh_prevents_server_start(tmp_path: Path) -> None:
    started = tmp_path / "server-started"

    result = await supervise(
        db_path=tmp_path / "missing.duckdb",
        server_command=_python(
            "import sys; from pathlib import Path; Path(sys.argv[1]).touch()",
            started,
        ),
        refresh_command=_python("import sys; sys.exit(9)"),
        refresh_interval=timedelta(days=1),
        retry_interval=timedelta(hours=1),
    )

    assert result == 9
    assert not started.exists()


def test_naive_duckdb_timestamp_is_treated_as_utc() -> None:
    delay = _seconds_until_due(
        datetime.now(UTC).replace(tzinfo=None),
        timedelta(hours=1),
    )
    assert 3595 < delay <= 3600
