"""HTML parsers that turn raw CADO pages into Pydantic domain models."""

from .company import (
    CompanyParseError,
    extract_company_number,
    parse_company_details,
    parse_company_search_results,
    parse_search_response,
)
from .lobbyist import (
    LobbyistParseError,
    all_row_indices,
    get_total_records,
    get_viewing_range,
    has_next_page,
    parse_lobbyist_detail,
    parse_lobbyist_search_results,
)

__all__ = [
    "CompanyParseError",
    "LobbyistParseError",
    "all_row_indices",
    "extract_company_number",
    "get_total_records",
    "get_viewing_range",
    "has_next_page",
    "parse_company_details",
    "parse_company_search_results",
    "parse_lobbyist_detail",
    "parse_lobbyist_search_results",
    "parse_search_response",
]
