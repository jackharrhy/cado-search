"""Smoke tests for the Typer CLI.

We test argument wiring and the ``info`` / ``ingest`` paths that touch real
files. The actual scrape commands shell out to network code that is already
covered by ``test_scrape_*`` and ``test_live_*``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cado.cli import app
from cado.storage import HtmlCache

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``settings.data_dir`` (via env var) at a tmpdir for every test."""
    monkeypatch.setenv("CADO_DATA_DIR", str(tmp_path))
    # Reload the settings module so the new env var is picked up.
    import importlib

    import cado.settings as settings_module

    importlib.reload(settings_module)
    # Reload modules that captured ``settings`` at import time.
    import cado.cli
    import cado.db.session
    import cado.storage

    importlib.reload(cado.storage)
    importlib.reload(cado.db.session)
    importlib.reload(cado.cli)
    return tmp_path


def test_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Scrape, store, and serve" in result.output


def test_info_with_no_data(runner: CliRunner) -> None:
    from cado.cli import app as fresh_app

    result = runner.invoke(fresh_app, ["info"])
    assert result.exit_code == 0
    assert "does not exist yet" in result.output


def test_ingest_companies_end_to_end(runner: CliRunner, _isolated_data_dir: Path) -> None:
    # Drop a fixture into the cache, then run ``cado ingest companies``.
    cache = HtmlCache(registry="companies")
    cache.write(
        "50000",
        (FIXTURES / "companies/c_50000_active_with_directors.html").read_text(),
    )
    from cado.cli import app as fresh_app

    result = runner.invoke(fresh_app, ["ingest", "companies"])
    assert result.exit_code == 0
    assert "Ingested 1 companies" in result.output

    # And confirm the DB now reports that one row.
    info = runner.invoke(fresh_app, ["info"])
    assert info.exit_code == 0
    assert "DuckDB: companies" in info.output


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


def _populate(_isolated_data_dir: Path) -> tuple[HtmlCache, HtmlCache]:
    """Seed both caches and the DuckDB with a single fixture each."""
    co_cache = HtmlCache(registry="companies")
    lo_cache = HtmlCache(registry="lobbyists")
    co_cache.write(
        "50000",
        (FIXTURES / "companies/c_50000_active_with_directors.html").read_text(),
    )
    lo_cache.write(
        "IHL-867-1005",
        (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text(),
    )
    return co_cache, lo_cache


class TestCleanDb:
    def test_no_op_when_no_db(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        from cado.cli import app as fresh_app

        result = runner.invoke(fresh_app, ["clean", "db", "--yes"])
        assert result.exit_code == 0
        assert "Nothing to do" in result.output

    def test_drops_db_keeps_cache(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        co_cache, _ = _populate(_isolated_data_dir)
        from cado.cli import app as fresh_app

        # Bring the database into existence by ingesting.
        runner.invoke(fresh_app, ["ingest", "companies"])
        from cado.settings import settings as fresh_settings

        assert fresh_settings.duckdb_path.exists()

        result = runner.invoke(fresh_app, ["clean", "db", "--yes"])
        assert result.exit_code == 0
        assert "Deleted DuckDB" in result.output
        assert not fresh_settings.duckdb_path.exists()
        # Cache survives.
        assert co_cache.exists("50000")

    def test_aborts_without_yes_and_no_confirmation(
        self, runner: CliRunner, _isolated_data_dir: Path
    ) -> None:
        _populate(_isolated_data_dir)
        from cado.cli import app as fresh_app

        runner.invoke(fresh_app, ["ingest", "companies"])
        from cado.settings import settings as fresh_settings

        # ``input='n\n'`` declines the confirmation prompt.
        result = runner.invoke(fresh_app, ["clean", "db"], input="n\n")
        assert result.exit_code == 1
        assert "Aborted" in result.output
        assert fresh_settings.duckdb_path.exists()


class TestCleanCache:
    def test_no_op_when_cache_empty(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        from cado.cli import app as fresh_app

        result = runner.invoke(fresh_app, ["clean", "cache", "--yes"])
        assert result.exit_code == 0
        assert "Nothing to do" in result.output

    def test_drops_cache_keeps_db(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        co_cache, lo_cache = _populate(_isolated_data_dir)
        from cado.cli import app as fresh_app

        runner.invoke(fresh_app, ["ingest", "companies"])
        from cado.settings import settings as fresh_settings

        assert co_cache.exists("50000")
        assert fresh_settings.duckdb_path.exists()

        result = runner.invoke(fresh_app, ["clean", "cache", "--yes"])
        assert result.exit_code == 0
        assert "Deleted" in result.output
        # Both registry caches are gone.
        assert not co_cache.exists("50000")
        assert not lo_cache.exists("IHL-867-1005")
        # DuckDB survives.
        assert fresh_settings.duckdb_path.exists()


class TestCleanAll:
    def test_wipes_everything(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        co_cache, lo_cache = _populate(_isolated_data_dir)
        from cado.cli import app as fresh_app

        runner.invoke(fresh_app, ["ingest", "companies"])
        from cado.settings import settings as fresh_settings

        result = runner.invoke(fresh_app, ["clean", "all", "--yes"])
        assert result.exit_code == 0
        assert "Deleted everything" in result.output
        assert not co_cache.exists("50000")
        assert not lo_cache.exists("IHL-867-1005")
        assert not fresh_settings.duckdb_path.exists()

    def test_no_op_when_nothing_present(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        from cado.cli import app as fresh_app

        result = runner.invoke(fresh_app, ["clean", "all", "--yes"])
        assert result.exit_code == 0
        assert "no data on disk" in result.output

    def test_aborts_without_confirmation(self, runner: CliRunner, _isolated_data_dir: Path) -> None:
        co_cache, _ = _populate(_isolated_data_dir)
        from cado.cli import app as fresh_app

        runner.invoke(fresh_app, ["ingest", "companies"])
        result = runner.invoke(fresh_app, ["clean", "all"], input="n\n")
        assert result.exit_code == 1
        assert "Aborted" in result.output
        # Nothing was touched.
        assert co_cache.exists("50000")
