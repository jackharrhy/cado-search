"""Tests for the company / condo / co-op detail page parser.

Every fixture in ``tests/fixtures/companies/`` is captured directly from
production. The behavioural tests below describe what each fixture is
supposed to demonstrate (an active local company with directors, a dissolved
old one, a condominium, etc.) so that when the parser is refactored we know
what we're validating.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cado.models import Category, CorporationType
from cado.parsers import (
    CompanyParseError,
    parse_company_details,
    parse_company_search_results,
    parse_search_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "companies"


def fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------


class TestActiveCompanyWithDirectors:
    """c_50000: CONNAIGRE NET INCORPORATED -- active local company, 4 directors."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_50000_active_with_directors.html"))

    def test_identity(self, company):
        assert company.number == "50000"
        assert company.name == "CONNAIGRE NET INCORPORATED"
        assert company.corporation_type is CorporationType.COMPANY
        assert company.category is Category.LOCAL

    def test_status_and_classification(self, company):
        assert company.status == "Active"
        assert company.business_type == "Without Share Capital"
        assert company.filing_type == "Incorporation Without Share Capital"
        assert company.incorporation_jurisdiction == "NL"
        assert company.min_max_directors == "3 / 12"

    def test_dates(self, company):
        assert company.incorporation_date == date(2004, 7, 23)
        assert company.last_annual_return == date(2025, 6, 30)
        assert company.registration_date is None

    def test_registered_office(self, company):
        ro = company.registered_office
        assert ro.contact == "c/o Town of Harbour Breton"
        assert ro.line1 == "41 Canada Drive"
        assert ro.city == "Harbour Breton"
        assert ro.province_state == "NL"  # trailing space stripped
        assert ro.country == "Canada"
        assert ro.postal_zip == "A0H 1P0"

    def test_mailing_address(self, company):
        ma = company.mailing_address
        assert ma.line1 == "P.O. Box 130"
        assert ma.city == "Harbour Breton"
        assert company.mailing_same_as_registered is False

    def test_directors(self, company):
        names = {d.full_name for d in company.directors}
        assert names == {"Mark Courtney", "Steven Crewe", "Miranda Maddox", "John Vallis"}
        # First/last split sanity check
        for d in company.directors:
            assert d.first_name is not None
            assert d.last_name is not None
            assert d.full_name == f"{d.first_name} {d.last_name}"


class TestDissolvedOldCompany:
    """c_10000: dissolved before 2004, addresses largely empty."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_10000_dissolved_old.html"))

    def test_status(self, company):
        assert company.status == "Dissolved (Prior To June 2004)"

    def test_addresses_handle_old_style_data(self, company):
        # Pre-2004 records sometimes cram an entire address into ``line1``
        # ("PORTUGAL COVE, NF") rather than using the structured city /
        # province / postal fields. The parser should round-trip the data
        # rather than try to "fix" it.
        ro = company.registered_office
        assert ro.line1 == "PORTUGAL COVE, NF"
        assert ro.city is None
        assert ro.postal_zip is None
        # Mailing on the same record is fully structured, which is a quirk
        # of the data, not the parser:
        ma = company.mailing_address
        assert ma.line1 == "P.O. BOX 125"
        assert ma.city == "PORTUGAL COVE"
        assert ma.postal_zip == "A0A 3K0"

    def test_no_directors(self, company):
        assert company.directors == []


class TestExtraProvincial:
    """c_99000: Irving Energy Inc. -- registered in NL but incorporated in NB."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_99000_extraprov_active.html"))

    def test_category(self, company):
        assert company.category is Category.EXTRA_PROVINCIAL
        assert company.incorporation_jurisdiction == "NB"
        assert company.business_type == "With Share Capital - Foreign"
        assert company.filing_type == "Registration"

    def test_both_dates_present(self, company):
        # Extra-provincial records carry both registration *and* incorporation.
        assert company.registration_date == date(2025, 10, 20)
        assert company.incorporation_date == date(2025, 9, 11)

    def test_mailing_in_other_province(self, company):
        assert company.mailing_address.province_state == "NB"
        assert company.registered_office.province_state == "NL"


class TestDiscontinued:
    """c_85000: discontinued (moved to a different jurisdiction). Mailing
    address declared "Same as Registered Office"."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_85000_discontinued.html"))

    def test_status_and_additional_info(self, company):
        assert company.status == "Discontinued"
        assert company.additional_info is not None
        assert "Quebec Business Corporations Act" in company.additional_info

    def test_mailing_same_as_registered_flag(self, company):
        assert company.mailing_same_as_registered is True


class TestSuffixedNumber:
    """c_2D: IMPERIAL TOBACCO LIMITED -- a legacy record whose canonical id is
    not a bare integer but a digit+suffix string. The detail parser must keep
    the number as ``"2D"``."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_2D_extraprov_old.html"))

    def test_number_preserves_suffix(self, company):
        assert company.number == "2D"

    def test_name(self, company):
        assert company.name == "IMPERIAL TOBACCO LIMITED"


class TestCondominium:
    """c_73498: a real condominium corporation."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_73498_condo.html"))

    def test_discriminated_as_condominium(self, company):
        assert company.corporation_type is CorporationType.CONDOMINIUM
        assert company.name.strip() == "10 Mile Condominium Corporation"

    def test_status(self, company):
        assert company.status == "Active"


class TestCooperative:
    """c_69963: cancelled co-operative."""

    @pytest.fixture
    def company(self):
        return parse_company_details(fx("c_69963_coop_cancelled.html"))

    def test_discriminated_as_cooperative(self, company):
        assert company.corporation_type is CorporationType.COOPERATIVE
        assert company.status == "Cancelled"


# ---------------------------------------------------------------------------
# Search results parsing
# ---------------------------------------------------------------------------


class TestSearchResultsMultiRow:
    """n_1_multirow: number=1 -> 4 result rows.

    Empirically, low-numbered records share a digit prefix but use uppercase
    letter suffixes (``"1I"``, ``"1D"``, ``"1F"``) to identify distinct
    legacy filings. The bare ``"1"`` is one of those records too. All four
    are separate companies despite sharing the prefix.
    """

    @pytest.fixture
    def results(self):
        return parse_company_search_results(fx("n_1_multirow.html"))

    def test_total_count(self, results):
        assert results.total == 4
        assert len(results.hits) == 4

    def test_numbers_have_suffixes(self, results):
        numbers = {hit.number for hit in results.hits}
        assert numbers == {"1I", "1D", "1F", "1"}

    def test_row_indices_are_one_based(self, results):
        # Used as the __doPostBack target -> rpt..._ctlN_lbtCompanyNumber
        assert {h.row_index for h in results.hits} == {1, 2, 3, 4}

    def test_corporation_types_present(self, results):
        # Every row should carry the discriminator.
        assert all(h.corporation_type for h in results.hits)


class TestParseSearchResponseDispatch:
    """parse_search_response classifies based on final URL + page contents."""

    def test_details_branch(self):
        resp = parse_search_response(
            fx("c_50000_active_with_directors.html"),
            final_url="https://cado.eservices.gov.nl.ca/Company/CompanyDetails.aspx",
        )
        assert resp.kind == "details"
        assert resp.details is not None
        assert resp.details.number == "50000"

    def test_hits_branch(self):
        resp = parse_search_response(
            fx("n_1_multirow.html"),
            final_url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        )
        assert resp.kind == "hits"
        assert resp.hits is not None
        assert resp.hits.total == 4


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_parse_company_details_rejects_non_detail_page() -> None:
    with pytest.raises(CompanyParseError, match="lblCompanyName"):
        parse_company_details("<html><body>not a detail page</body></html>")
