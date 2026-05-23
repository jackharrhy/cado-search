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
