"""Tests for the DuckDB ingest layer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from cado.db import (
    connect,
    ingest_companies,
    ingest_lobbyists,
    ingest_one_html,
)
from cado.storage import HtmlCache

FIXTURES = Path(__file__).parent / "fixtures"
COMPANIES = FIXTURES / "companies"


def fx(name: str) -> str:
    return (COMPANIES / name).read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    return connect(tmp_path / "cado.duckdb")


@pytest.fixture
def cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="companies")


@pytest.fixture
def lobby_cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="lobbyists")


class TestSchema:
    def test_all_tables_exist(self, conn: duckdb.DuckDBPyConnection) -> None:
        names = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        assert {
            "companies",
            "company_directors",
            "company_previous_names",
            "company_historical_remarks",
            "lobbyist_registrations",
            "ingest_log",
        } <= names


class TestIngestOneCompany:
    def test_inserts_a_row(self, conn: duckdb.DuckDBPyConnection, cache: HtmlCache) -> None:
        cache.write("50000", fx("c_50000_active_with_directors.html"))
        results = list(ingest_companies(conn, cache))
        assert len(results) == 1
        assert results[0].parsed_ok is True

        row = conn.execute(
            "SELECT number, name, corporation_type, status, incorporation_date "
            "FROM companies WHERE number = '50000'"
        ).fetchone()
        assert row is not None
        assert row[0] == "50000"
        assert row[1] == "CONNAIGRE NET INCORPORATED"
        assert row[2] == "Company"
        assert row[3] == "Active"
        assert row[4] == date(2004, 7, 23)

    def test_directors_become_rows(self, conn: duckdb.DuckDBPyConnection, cache: HtmlCache) -> None:
        cache.write("50000", fx("c_50000_active_with_directors.html"))
        list(ingest_companies(conn, cache))
        rows = conn.execute(
            "SELECT seq, full_name FROM company_directors "
            "WHERE company_number = '50000' ORDER BY seq"
        ).fetchall()
        names = [r[1] for r in rows]
        assert names == ["Mark Courtney", "Steven Crewe", "Miranda Maddox", "John Vallis"]
        # Sequences are dense and 1-based.
        assert [r[0] for r in rows] == [1, 2, 3, 4]

    def test_idempotent_on_reingest(
        self, conn: duckdb.DuckDBPyConnection, cache: HtmlCache
    ) -> None:
        cache.write("50000", fx("c_50000_active_with_directors.html"))
        list(ingest_companies(conn, cache))
        list(ingest_companies(conn, cache))  # re-ingest

        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone() == (1,)
        # Director rows are wiped+reinserted, so still exactly 4.
        assert conn.execute(
            "SELECT COUNT(*) FROM company_directors WHERE company_number = '50000'"
        ).fetchone() == (4,)


class TestIngestMultipleCompanies:
    def test_distinct_records_coexist(
        self, conn: duckdb.DuckDBPyConnection, cache: HtmlCache
    ) -> None:
        # Mix of types: company, condo, coop, suffixed.
        cache.write("50000", fx("c_50000_active_with_directors.html"))
        cache.write("73498", fx("c_73498_condo.html"))
        cache.write("69963", fx("c_69963_coop_cancelled.html"))
        cache.write("2D", fx("c_2D_extraprov_old.html"))
        cache.write("99000", fx("c_99000_extraprov_active.html"))

        results = list(ingest_companies(conn, cache))
        assert all(r.parsed_ok for r in results)

        # All five corp types accounted for.
        rows = conn.execute(
            "SELECT corporation_type, COUNT(*) FROM companies GROUP BY corporation_type"
        ).fetchall()
        counts = dict(rows)
        assert counts == {"Company": 3, "Condominium": 1, "Co-operative": 1}

        # The suffixed number survives.
        n = conn.execute("SELECT name FROM companies WHERE number = '2D'").fetchone()
        assert n == ("IMPERIAL TOBACCO LIMITED",)


class TestIngestLobbyist:
    def test_one_record(self, conn: duckdb.DuckDBPyConnection, lobby_cache: HtmlCache) -> None:
        html = (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text()
        lobby_cache.write("IHL-867-1005", html)
        results = list(ingest_lobbyists(conn, lobby_cache))
        assert results[0].parsed_ok

        row = conn.execute(
            "SELECT registration_number, contact_name, firm_name, lobbyist_type, "
            "status, raw_fields "
            "FROM lobbyist_registrations WHERE registration_number = 'IHL-867-1005'"
        ).fetchone()
        assert row is not None
        assert row[0] == "IHL-867-1005"
        assert row[1] == "Rhonda Tulk-Lane"
        assert row[2] == "Atlantic Chamber of Commerce"
        assert row[3] == "In-House"
        assert row[4] == "Approved"
        # raw_fields round-trips through JSON.
        raw = json.loads(row[5])
        assert "lblOrgMembership" in raw


class TestIngestLog:
    def test_records_every_attempt(self, conn: duckdb.DuckDBPyConnection, cache: HtmlCache) -> None:
        cache.write("50000", fx("c_50000_active_with_directors.html"))
        cache.write("garbage", "<html><body>not a valid record</body></html>")
        list(ingest_companies(conn, cache))

        rows = conn.execute(
            "SELECT record_key, parsed_ok, error FROM ingest_log "
            "WHERE kind = 'company' ORDER BY record_key"
        ).fetchall()
        keys = {r[0]: r for r in rows}
        assert keys["50000"][1] is True
        assert keys["50000"][2] is None
        assert keys["garbage"][1] is False
        assert "lblCompanyName" in (keys["garbage"][2] or "")


class TestDispatch:
    def test_ingest_one_html_dispatches_by_kind(self, conn: duckdb.DuckDBPyConnection) -> None:
        co_html = fx("c_50000_active_with_directors.html")
        result = ingest_one_html(conn, "company", "50000", co_html)
        assert result.parsed_ok

        lob_html = (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text()
        result = ingest_one_html(conn, "lobbyist", "IHL-867-1005", lob_html)
        assert result.parsed_ok

    def test_unknown_kind_raises(self, conn: duckdb.DuckDBPyConnection) -> None:
        with pytest.raises(ValueError, match="unknown ingest kind"):
            ingest_one_html(conn, "spiders", "x", "<html/>")
