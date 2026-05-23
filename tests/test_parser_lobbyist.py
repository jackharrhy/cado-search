"""Tests for the lobbyist parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cado.parsers import (
    LobbyistParseError,
    get_total_records,
    has_next_page,
    parse_lobbyist_detail,
    parse_lobbyist_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestLobbyistDetail:
    """IHL-867-1005: Rhonda Tulk-Lane, Atlantic Chamber of Commerce."""

    @pytest.fixture
    def reg(self):
        return parse_lobbyist_detail(fx("lobbyist_summary_IHL-867-1005.html"))

    def test_identity(self, reg):
        assert reg.registration_number == "IHL-867-1005"
        assert reg.status == "Approved"

    def test_lobbyist_type_inferred_from_prefix(self, reg):
        assert reg.lobbyist_type == "In-House"

    def test_dates(self, reg):
        assert reg.registration_date == date(2026, 5, 7)
        assert reg.effective_date == date(2026, 6, 16)
        assert reg.amended_date == date(2026, 5, 7)
        assert reg.approval_date == date(2026, 5, 11)

    def test_contact_info(self, reg):
        assert reg.contact_name == "Rhonda Tulk-Lane"
        assert reg.contact_address.line1 == "113 Salmonier Line"
        assert reg.contact_address.city == "Holyrood"
        assert reg.contact_address.postal_zip == "A0A 2R0"
        assert "Newfoundland" in (reg.contact_address.province_state or "")

    def test_firm_info(self, reg):
        assert reg.firm_name == "Atlantic Chamber of Commerce"
        assert reg.firm_address.line1 == "113 Salmonier Line"
        assert reg.firm_address.city == "Holyrood"

    def test_free_form_text_preserves_newlines(self, reg):
        assert reg.particulars is not None
        assert "red tape" in reg.particulars
        assert reg.organization_description is not None
        # Multi-line text is preserved.
        assert "\n" in reg.organization_description

    def test_raw_fields_includes_everything(self, reg):
        # The raw_fields dict carries every label we extracted, even those
        # the typed model doesn't expose.
        assert "lblRegistrationNumber" in reg.raw_fields
        assert "lblOrgMembership" in reg.raw_fields


def test_lobbyist_detail_rejects_unrelated_page() -> None:
    with pytest.raises(LobbyistParseError):
        parse_lobbyist_detail("<html><body>not a lobbyist page</body></html>")


class TestLobbyistSearchResults:
    @pytest.fixture
    def hits(self):
        return parse_lobbyist_search_results(fx("lobbyist_search_all_page1.html"))

    def test_ten_rows(self, hits):
        assert len(hits) == 10

    def test_first_row_is_the_known_record(self, hits):
        first = hits[0]
        assert first.registration_number == "IHL-867-1005"
        assert first.row_index == 1
        assert "Tulk-Lane" in first.name
        assert first.lobbyist_type == "In-House"

    def test_row_indices_are_sequential(self, hits):
        assert [h.row_index for h in hits] == list(range(1, 11))

    def test_total_records(self):
        total = get_total_records(fx("lobbyist_search_all_page1.html"))
        assert total == 727

    def test_has_next_page_on_first_page(self):
        assert has_next_page(fx("lobbyist_search_all_page1.html")) is True

    def test_no_next_page_on_last_page(self):
        # The upstream keeps rendering ``Next >`` even on the last page; the
        # pager treats it as a no-op. Detection therefore uses viewing-range
        # vs total, not the presence of the link.
        assert has_next_page(fx("lobbyist_search_all_last_page.html")) is False
