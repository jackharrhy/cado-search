"""Contract tests for the transport-independent registry query service."""

from __future__ import annotations

from pathlib import Path

import pytest

from cado.models import Category, CorporationType, LobbyistType
from cado.query import RegistryQueryService


@pytest.fixture
def service(seeded_db: Path) -> RegistryQueryService:
    return RegistryQueryService(seeded_db, public_base_url="https://cado.example")


class TestCompanyQueries:
    def test_search_by_name_and_exact_number(self, service: RegistryQueryService) -> None:
        by_name = service.search_companies(query="irving")
        by_number = service.search_companies(query="50000")

        assert by_name.total == 1
        assert by_name.items[0].number == "99000"
        assert by_name.items[0].record_url == "https://cado.example/company/99000"
        assert by_number.items[0].name == "CONNAIGRE NET INCORPORATED"

    def test_filters_and_paginates_stably(self, service: RegistryQueryService) -> None:
        companies = service.search_companies(
            corporation_type=CorporationType.COMPANY,
            category=Category.EXTRA_PROVINCIAL,
            status="active",
            limit=1,
        )

        assert companies.total == 1
        assert companies.returned == 1
        assert companies.next_offset is None
        assert companies.items[0].number == "99000"

        first = service.search_companies(limit=2)
        second = service.search_companies(limit=2, offset=first.next_offset or 0)
        assert first.total == 5
        assert first.next_offset == 2
        assert {item.number for item in first.items}.isdisjoint(
            item.number for item in second.items
        )

    def test_detail_reconstructs_relations_and_provenance(
        self, service: RegistryQueryService
    ) -> None:
        record = service.get_company("50000")

        assert record is not None
        assert record.registered_office.city == "Harbour Breton"
        assert [director.full_name for director in record.directors] == [
            "Mark Courtney",
            "Steven Crewe",
            "Miranda Maddox",
            "John Vallis",
        ]
        assert record.record_url == "https://cado.example/company/50000"
        assert record.ingested_at is not None
        assert record.snapshot_id == "test-snapshot"
        assert record.source_fetched_at is not None

    def test_missing_detail_is_none(self, service: RegistryQueryService) -> None:
        assert service.get_company("NOPE") is None

    @pytest.mark.parametrize(
        ("limit", "offset", "message"),
        [(0, 0, "limit"), (51, 0, "limit"), (20, -1, "offset")],
    )
    def test_rejects_unbounded_pages(
        self,
        service: RegistryQueryService,
        limit: int,
        offset: int,
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            service.search_companies(limit=limit, offset=offset)


class TestLobbyistQueries:
    def test_search_filters_and_detail(self, service: RegistryQueryService) -> None:
        page = service.search_lobbyists(
            query="Atlantic",
            lobbyist_type=LobbyistType.IN_HOUSE,
            status="approved",
        )

        assert page.total == 1
        assert page.items[0].registration_number == "IHL-867-1005"
        assert page.items[0].record_url.endswith("/lobbyist/IHL-867-1005")

        record = service.get_lobbyist("IHL-867-1005")
        assert record is not None
        assert record.contact_name == "Rhonda Tulk-Lane"
        assert record.firm_name == "Atlantic Chamber of Commerce"
        assert "lblOrgMembership" in record.raw_fields
        assert record.subject_matters[0].name == "Economic Development"
        assert record.in_house_lobbyists[0].name == "Tulk-Lane, Rhonda"


def test_dataset_status_reports_each_registry(service: RegistryQueryService) -> None:
    status = service.get_dataset_status()

    assert status.companies.count == 3
    assert status.condominiums.count == 1
    assert status.cooperatives.count == 1
    assert status.lobbyists.count == 1
    assert status.snapshot_id == "test-snapshot"
    assert status.source_fetched_at is not None
    assert status.snapshot_built_at is not None
    assert status.published_at is not None
    assert "mirror" in status.notice.lower()
