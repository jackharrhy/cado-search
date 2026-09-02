"""Integration tests for the FastAPI app.

We seed a temporary DuckDB with our fixture HTML and assert the UI returns
the right rows for various queries.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cado.api import create_app
from cado.db import connect


@pytest.fixture
def client(seeded_db: Path):
    app = create_app(seeded_db)
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
        assert "test-snapshot" in response.text

    def test_registry_tabs_keep_each_search_on_its_own_page(self, client: TestClient) -> None:
        companies = client.get("/")
        lobbyists = client.get("/lobbyists")

        assert 'class="active" aria-current="page">Companies' in companies.text
        assert 'id="company-filter-form"' in companies.text
        assert 'id="lobbyist-filter-form"' not in companies.text
        assert 'href="/lobbyists"' in companies.text

        assert lobbyists.status_code == 200
        assert 'class="active" aria-current="page">Lobbyists' in lobbyists.text
        assert 'id="lobbyist-filter-form"' in lobbyists.text
        assert 'id="company-filter-form"' not in lobbyists.text
        assert '>1</span><span class="label">Lobbyist registrations' in lobbyists.text

    def test_each_visible_company_column_has_filters(self, client: TestClient) -> None:
        response = client.get("/")

        for field in (
            "name",
            "number",
            "corp_type",
            "status",
            "category",
            "incorporated_from",
            "incorporated_to",
            "city",
            "province_state",
        ):
            assert f'name="{field}"' in response.text

        for field in (
            "name",
            "number",
            "corporation_type",
            "status",
            "category",
            "incorporation_date",
            "location",
        ):
            assert f'data-sort="{field}"' in response.text

    def test_each_visible_lobbyist_column_has_filters(self, client: TestClient) -> None:
        response = client.get("/lobbyists")

        for field in (
            "registration_number",
            "contact_name",
            "firm_name",
            "lobbyist_type",
            "status",
            "effective_from",
            "effective_to",
        ):
            assert f'name="{field}"' in response.text

        for field in (
            "registration_number",
            "contact_name",
            "firm_name",
            "lobbyist_type",
            "status",
            "effective_date",
        ):
            assert f'data-sort="{field}"' in response.text

    def test_company_locations_are_type_ahead_options(self, client: TestClient) -> None:
        response = client.get("/")

        assert 'list="company-cities"' in response.text
        assert '<option value="Harbour Breton">' in response.text
        assert '<option value="St. John&#39;s">' in response.text
        assert 'list="company-provinces"' in response.text
        assert '<option value="NL">' in response.text

    def test_snapshot_dates_are_human_readable(self, client: TestClient) -> None:
        response = client.get("/")

        assert re.search(
            r"Source data fetched <time datetime=\"\d{4}-\d{2}-\d{2}\">"
            r"[A-Z][a-z]+ \d{1,2}, \d{4}</time>;\s+published",
            response.text,
        )
        assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", response.text)

    def test_sort_indicators_use_active_ascii_arrows(self, client: TestClient) -> None:
        stylesheet = client.get("/static/style.css").text

        assert 'th[aria-sort="none"] .sort-indicator' not in stylesheet
        assert 'content: "^"' in stylesheet
        assert 'content: "v"' in stylesheet
        assert not any(symbol in stylesheet for symbol in ("↕", "↑", "↓"))

    def test_health_endpoints(self, client: TestClient) -> None:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {
            "status": "ok",
            "snapshot_id": "test-snapshot",
        }


class TestCompanySearch:
    def test_blank_query_returns_all(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"sort": ""})
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

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"name": "Irving"}, "Irving Energy Inc."),
            ({"number": "2D"}, "IMPERIAL TOBACCO LIMITED"),
            ({"corp_type": "Condominium"}, "10 Mile Condominium Corporation"),
            ({"category": "Extra-Provincial"}, "Irving Energy Inc."),
            (
                {"incorporated_from": "2025-01-01", "incorporated_to": "2025-12-31"},
                "Irving Energy Inc.",
            ),
            ({"city": "Harbour Breton", "province_state": "NL"}, "CONNAIGRE NET"),
        ],
    )
    def test_column_filters(
        self,
        client: TestClient,
        params: dict[str, str],
        expected: str,
    ) -> None:
        response = client.get("/search/companies", params=params)

        assert response.status_code == 200
        assert expected in response.text
        assert 'class="result-meta-row"' in response.text
        assert "<table" not in response.text

    def test_empty_state(self, client: TestClient) -> None:
        response = client.get("/search/companies", params={"q": "zzzzzNotARealName"})
        assert "No matches" in response.text

    def test_each_sort_field_and_direction_is_supported(self, client: TestClient) -> None:
        for field in (
            "name",
            "number",
            "corporation_type",
            "status",
            "category",
            "incorporation_date",
            "location",
        ):
            for direction in ("asc", "desc"):
                response = client.get(
                    "/search/companies",
                    params={"sort": field, "direction": direction},
                )
                assert response.status_code == 200, (field, direction, response.text)

    def test_sort_direction_changes_company_order(self, client: TestClient) -> None:
        name_asc = client.get("/search/companies", params={"sort": "name", "direction": "asc"}).text
        name_desc = client.get(
            "/search/companies", params={"sort": "name", "direction": "desc"}
        ).text
        assert name_asc.index("10 Mile Condominium") < name_asc.index("Irving Energy")
        assert name_desc.index("Irving Energy") < name_desc.index("10 Mile Condominium")

        number_asc = client.get(
            "/search/companies", params={"sort": "number", "direction": "asc"}
        ).text
        assert number_asc.index(">2D<") < number_asc.index(">50000<")

        date_desc = client.get(
            "/search/companies",
            params={"sort": "incorporation_date", "direction": "desc"},
        ).text
        assert date_desc.index("Irving Energy") < date_desc.index("CONNAIGRE NET")
        assert date_desc.index("CONNAIGRE NET") < date_desc.index("IMPERIAL TOBACCO")

    def test_rejects_unknown_sort_values(self, client: TestClient) -> None:
        assert client.get("/search/companies", params={"sort": "drop table"}).status_code == 422
        assert (
            client.get(
                "/search/companies", params={"sort": "name", "direction": "sideways"}
            ).status_code
            == 422
        )


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

    def test_404_does_not_reflect_unescaped_path_input(self, client: TestClient) -> None:
        response = client.get("/company/%3Cimg%20src=x%20onerror=alert(1)%3E")
        assert response.status_code == 404
        assert "<img" not in response.text


class TestLobbyistEndpoints:
    def test_search(self, client: TestClient) -> None:
        response = client.get("/search/lobbyists", params={"q": "Atlantic"})
        assert response.status_code == 200
        assert "IHL-867-1005" in response.text

    def test_blank_sort_returns_all(self, client: TestClient) -> None:
        response = client.get("/search/lobbyists", params={"sort": ""})

        assert response.status_code == 200
        assert "IHL-867-1005" in response.text

    @pytest.mark.parametrize(
        "params",
        [
            {"registration_number": "IHL-867-1005"},
            {"contact_name": "Rhonda"},
            {"firm_name": "Atlantic Chamber"},
            {"lobbyist_type": "In-House"},
            {"status": "Approved"},
            {"effective_from": "2026-01-01", "effective_to": "2026-12-31"},
        ],
    )
    def test_column_filters(self, client: TestClient, params: dict[str, str]) -> None:
        response = client.get("/search/lobbyists", params=params)

        assert response.status_code == 200
        assert "IHL-867-1005" in response.text
        assert 'class="result-meta-row"' in response.text
        assert "<table" not in response.text

    def test_detail(self, client: TestClient) -> None:
        response = client.get("/lobbyist/IHL-867-1005")
        assert response.status_code == 200
        assert "Rhonda Tulk-Lane" in response.text
        assert "Atlantic Chamber of Commerce" in response.text
        assert "Economic Development" in response.text
        assert "Tulk-Lane, Rhonda" in response.text
        assert 'href="/lobbyists"' in response.text

    def test_detail_404(self, client: TestClient) -> None:
        response = client.get("/lobbyist/NOPE-000-000")
        assert response.status_code == 404

    def test_each_sort_field_and_direction_is_supported(self, client: TestClient) -> None:
        for field in (
            "registration_number",
            "contact_name",
            "firm_name",
            "lobbyist_type",
            "status",
            "effective_date",
        ):
            for direction in ("asc", "desc"):
                response = client.get(
                    "/search/lobbyists",
                    params={"sort": field, "direction": direction},
                )
                assert response.status_code == 200, (field, direction, response.text)


class TestStartupRequiresDatabase:
    def test_raises_if_database_missing(self, tmp_path: Path) -> None:
        app = create_app(tmp_path / "missing.duckdb")
        # Lifespan errors surface on first request via TestClient.
        with pytest.raises(RuntimeError, match="does not exist"), TestClient(app):
            pass

    def test_rejects_database_without_published_metadata(self, tmp_path: Path) -> None:
        db_path = tmp_path / "unpublished.duckdb"
        connect(db_path).close()
        app = create_app(db_path)
        with pytest.raises(RuntimeError, match="no metadata"), TestClient(app):
            pass
