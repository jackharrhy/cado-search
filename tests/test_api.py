"""Integration tests for the FastAPI app.

We seed a temporary DuckDB with our fixture HTML and assert the UI returns
the right rows for various queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cado.api import create_app
from cado.db import connect, ingest_one_html

FIXTURES = Path(__file__).parent / "fixtures"
COMPANIES = FIXTURES / "companies"


def _seed_db(path: Path) -> None:
    """Build a tiny but representative DuckDB at ``path``."""
    conn = connect(path)
    # 5 companies covering each type / status diversity.
    for key, fname in [
        ("50000", "c_50000_active_with_directors.html"),
        ("73498", "c_73498_condo.html"),
        ("69963", "c_69963_coop_cancelled.html"),
        ("2D", "c_2D_extraprov_old.html"),
        ("99000", "c_99000_extraprov_active.html"),
    ]:
        html = (COMPANIES / fname).read_text()
        ingest_one_html(conn, "company", key, html)
    # 1 lobbyist
    lob_html = (FIXTURES / "lobbyist_summary_IHL-867-1005.html").read_text()
    ingest_one_html(conn, "lobbyist", "IHL-867-1005", lob_html)
    conn.close()


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "cado.duckdb"
    _seed_db(db_path)
    app = create_app(db_path)
    # Use as a context manager so the lifespan handler runs.
    with TestClient(app) as c:
        yield c


class TestIndex:
    def test_returns_counts(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        # 3 companies in the seed are corporation_type=Company.
        assert '>3</span><span class="label">Companies' in response.text
        assert '>1</span><span class="label">Condominiums' in response.text
        assert '>1</span><span class="label">Co-operatives' in response.text
        assert '>1</span><span class="label">Lobbyists' in response.text


class TestCompanySearch:
    def test_blank_query_returns_all(self, client: TestClient) -> None:
        response = client.get("/search/companies")
        assert response.status_code == 200
        # 5 companies seeded.
        assert "Showing all 5 matches" in response.text
        assert "CONNAIGRE NET INCORPORATED" in response.text

    def test_name_query_filters(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"q": "Irving"})
        assert response.status_code == 200
        assert "Showing all 1 match" in response.text
        assert "Irving Energy Inc." in response.text

    def test_number_query_exact_match(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"q": "50000"})
        assert response.status_code == 200
        assert "CONNAIGRE NET INCORPORATED" in response.text

    def test_corp_type_filter(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"corp_type": "Condominium"})
        assert response.status_code == 200
        assert "Showing all 1 match" in response.text
        assert "Condominium" in response.text

    def test_status_filter(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"status": "Cancelled"})
        assert "A-FRS COOPERATIVE" in response.text

    def test_empty_state(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"q": "zzzzzNotARealName"})
        assert "No matches" in response.text


class TestCompanyDetail:
    def test_renders_full_record(self, client: TestClient) -> None:
        response = client.get("/company/50000")
        assert response.status_code == 200
        assert "CONNAIGRE NET INCORPORATED" in response.text
        # Directors are listed.
        for name in ("Mark Courtney", "Steven Crewe", "Miranda Maddox", "John Vallis"):
            assert name in response.text
        # Address fields.
        assert "Harbour Breton" in response.text
        assert "A0H 1P0" in response.text

    def test_suffixed_number_route(self, client: TestClient) -> None:
        response = client.get("/company/2D")
        assert response.status_code == 200
        assert "IMPERIAL TOBACCO LIMITED" in response.text

    def test_404_when_missing(self, client: TestClient) -> None:
        response = client.get("/company/9999999")
        assert response.status_code == 404


class TestLobbyistEndpoints:
    def test_search(self, client: TestClient) -> None:
        response = client.get("/search/lobbyists", params={"q": "Atlantic"})
        assert response.status_code == 200
        assert "IHL-867-1005" in response.text

    def test_detail(self, client: TestClient) -> None:
        response = client.get("/lobbyist/IHL-867-1005")
        assert response.status_code == 200
        assert "Rhonda Tulk-Lane" in response.text
        assert "Atlantic Chamber of Commerce" in response.text

    def test_detail_404(self, client: TestClient) -> None:
        response = client.get("/lobbyist/NOPE-000-000")
        assert response.status_code == 404


class TestStartupRequiresDatabase:
    def test_raises_if_database_missing(self, tmp_path: Path) -> None:
        app = create_app(tmp_path / "missing.duckdb")
        # Lifespan errors surface on first request via TestClient.
        with pytest.raises(RuntimeError, match="does not exist"), TestClient(app):
            pass
