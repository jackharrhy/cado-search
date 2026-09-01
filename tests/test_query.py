"""Contract tests for the transport-independent registry query service."""

from __future__ import annotations

from pathlib import Path

import pytest

from cado.models import Category, CorporationType, LobbyistType
from cado.query import (
    AddressFilter,
    CompanySearchFilters,
    DateRange,
    LobbyingActivityFilter,
    LobbyistAddressFilter,
    LobbyistSearchFilters,
    MatchMode,
    RegistryQueryService,
    TextTermsFilter,
)


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
        assert by_name.items[0].query_matches[0].field == "current_name"
        assert by_name.items[0].query_matches[0].value == "Irving Energy Inc."
        assert by_number.items[0].name == "CONNAIGRE NET INCORPORATED"
        assert by_number.items[0].query_matches[0].field == "company_number"

    def test_search_by_affiliated_person_and_previous_name(
        self, service: RegistryQueryService
    ) -> None:
        by_director = service.search_companies(query="Mark Courtney")
        by_previous_name = service.search_companies(query="community net")

        assert by_director.total == 1
        assert by_director.items[0].number == "50000"
        assert by_director.items[0].query_matches[0].field == "current_director"
        assert by_director.items[0].query_matches[0].value == "Mark Courtney"
        assert by_previous_name.total == 1
        assert by_previous_name.items[0].query_matches[0].field == "previous_name"

    def test_filters_and_paginates_stably(self, service: RegistryQueryService) -> None:
        companies = service.search_companies(
            filters=CompanySearchFilters(
                corporation_types=[CorporationType.COMPANY],
                categories=[Category.EXTRA_PROVINCIAL],
                statuses=["active"],
            ),
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

    def test_compound_company_filters_have_explicit_all_semantics(
        self, service: RegistryQueryService
    ) -> None:
        page = service.search_companies(
            filters=CompanySearchFilters(
                director_names=TextTermsFilter(
                    terms=["Mark Courtney", "Steven Crewe"],
                    match=MatchMode.ALL,
                ),
                incorporation_date=DateRange.model_validate(
                    {"date_from": "2004-01-01", "date_to": "2004-12-31"}
                ),
                registered_office=AddressFilter(city="Harbour Breton"),
                has_current_directors=True,
            )
        )
        missing_one = service.search_companies(
            filters=CompanySearchFilters(
                director_names=TextTermsFilter(
                    terms=["Mark Courtney", "Nobody Here"],
                    match=MatchMode.ALL,
                )
            )
        )

        assert page.total == 1
        assert page.items[0].number == "50000"
        assert page.items[0].query_matches == []
        assert missing_one.total == 0

    def test_company_relation_and_text_filters(self, service: RegistryQueryService) -> None:
        previous = service.search_companies(
            filters=CompanySearchFilters(
                previous_names=TextTermsFilter(terms=["community"]),
                has_previous_names=True,
            )
        )
        historical = service.search_companies(
            filters=CompanySearchFilters(
                historical_remarks=TextTermsFilter(terms=["cancellation filed"]),
                has_historical_remarks=True,
                mailing_address=AddressFilter(city="Montreal"),
            )
        )

        assert [item.number for item in previous.items] == ["50000"]
        assert [item.number for item in historical.items] == ["2D"]

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
            filters=LobbyistSearchFilters(
                lobbyist_types=[LobbyistType.IN_HOUSE],
                statuses=["approved"],
            ),
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

    def test_searches_affiliated_people_and_filters_lobbying_activity(
        self, service: RegistryQueryService
    ) -> None:
        by_person = service.search_lobbyists(query="Tulk-Lane, Rhonda")
        filtered = service.search_lobbyists(
            filters=LobbyistSearchFilters(
                subject_matters=LobbyingActivityFilter(
                    terms=["Economic Development", "Energy"],
                    match=MatchMode.ALL,
                    has_lobbied=False,
                    expects_to_lobby=True,
                ),
                lobbying_targets=LobbyingActivityFilter(terms=["Office of the Premier"]),
                contact_address=LobbyistAddressFilter(city="Holyrood"),
                effective_date=DateRange.model_validate({"date_from": "2026-01-01"}),
                has_in_house_lobbyists=True,
            )
        )
        wrong_flag = service.search_lobbyists(
            filters=LobbyistSearchFilters(
                subject_matters=LobbyingActivityFilter(
                    terms=["Economic Development"],
                    has_lobbied=True,
                )
            )
        )

        assert by_person.total == 1
        assert by_person.items[0].query_matches[0].field == "in_house_lobbyist"
        assert filtered.total == 1
        assert wrong_flag.total == 0


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
