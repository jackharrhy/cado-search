"""HTML parsers that turn raw CADO pages into Pydantic domain models."""

from .company import (
    CompanyParseError,
    parse_company_details,
    parse_company_search_results,
    parse_search_response,
)

__all__ = [
    "CompanyParseError",
    "parse_company_details",
    "parse_company_search_results",
    "parse_search_response",
]
