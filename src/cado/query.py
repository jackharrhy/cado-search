"""Typed, read-only queries over the CADO DuckDB mirror.

This module is the application-facing boundary around DuckDB.  The HTML UI,
MCP server, and any future JSON API all consume the same response models and
query semantics instead of embedding SQL in their transport handlers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import connect
from .models import (
    Address,
    Category,
    Company,
    CorporationType,
    Director,
    InHouseLobbyist,
    LobbyingActivity,
    LobbyistRegistration,
    LobbyistType,
    PreviousName,
)
from .settings import settings

COMPANY_SOURCE_URL = "https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx"
LOBBYIST_SOURCE_URL = "https://cado.eservices.gov.nl.ca/Lobbyist/LobbyistSearch.aspx"
SOURCE_NOTICE = (
    "This is a mirror of the Government of Newfoundland and Labrador's public "
    "CADO registries and may not reflect the latest authoritative filing."
)

FilterTerm = Annotated[str, Field(min_length=1, max_length=200)]
FilterValue = Annotated[str, Field(min_length=1, max_length=200)]
SortDirection = Literal["asc", "desc"]
CompanySortField = Literal[
    "name",
    "number",
    "corporation_type",
    "status",
    "category",
    "incorporation_date",
    "location",
]
LobbyistSortField = Literal[
    "registration_number",
    "contact_name",
    "firm_name",
    "lobbyist_type",
    "status",
    "effective_date",
]

_COMPANY_SORT_EXPRESSIONS: dict[str, tuple[str, ...]] = {
    "name": ("NULLIF(lower(trim(c.name)), '')",),
    "number": (
        "TRY_CAST(regexp_extract(c.number, '^[0-9]+') AS BIGINT)",
        "lower(c.number)",
    ),
    "corporation_type": ("NULLIF(lower(trim(c.corporation_type)), '')",),
    "status": ("NULLIF(lower(trim(c.status)), '')",),
    "category": ("NULLIF(lower(trim(c.category)), '')",),
    "incorporation_date": ("c.incorporation_date",),
    "location": (
        "NULLIF(lower(trim(c.ro_city)), '')",
        "NULLIF(lower(trim(c.ro_province_state)), '')",
    ),
}

_LOBBYIST_SORT_EXPRESSIONS: dict[str, tuple[str, ...]] = {
    "registration_number": (
        "lower(regexp_extract(l.registration_number, '^[A-Za-z]+'))",
        "TRY_CAST(regexp_extract(l.registration_number, '([0-9]+)', 1) AS BIGINT)",
        "TRY_CAST(regexp_extract(l.registration_number, '([0-9]+)$', 1) AS BIGINT)",
        "lower(l.registration_number)",
    ),
    "contact_name": ("NULLIF(lower(trim(l.contact_name)), '')",),
    "firm_name": ("NULLIF(lower(trim(l.firm_name)), '')",),
    "lobbyist_type": ("NULLIF(lower(trim(l.lobbyist_type)), '')",),
    "status": ("NULLIF(lower(trim(l.status)), '')",),
    "effective_date": ("l.effective_date",),
}


class MatchMode(StrEnum):
    """How the terms inside one text filter are combined."""

    ANY = "any"
    ALL = "all"


class TextTermsFilter(BaseModel):
    """Case-insensitive substring terms with explicit any/all semantics."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    terms: list[FilterTerm] = Field(
        min_length=1,
        max_length=20,
        description="Case-insensitive literal substrings to match.",
        examples=[["Energy", "Trade"]],
    )
    match: MatchMode = Field(
        default=MatchMode.ANY,
        description=(
            "'any' accepts a record matching at least one term; 'all' requires every term. "
            "For related records such as directors, different terms may match different people."
        ),
    )


class LobbyingActivityFilter(TextTermsFilter):
    """Filter named lobbying activities and, optionally, their declared timing."""

    has_lobbied: bool | None = Field(
        default=None,
        description="When set, require the matched activity to have this has-lobbied value.",
    )
    expects_to_lobby: bool | None = Field(
        default=None,
        description="When set, require the matched activity to have this expected value.",
    )


class DateRange(BaseModel):
    """An inclusive ISO-8601 date range; either boundary may be omitted."""

    model_config = ConfigDict(extra="forbid")

    date_from: date | None = Field(
        default=None,
        description="Inclusive lower bound in YYYY-MM-DD form.",
        examples=["2020-01-01"],
    )
    date_to: date | None = Field(
        default=None,
        description="Inclusive upper bound in YYYY-MM-DD form.",
        examples=["2025-12-31"],
    )

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.date_from is None and self.date_to is None:
            raise ValueError("provide date_from, date_to, or both")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from must not be later than date_to")
        return self


class AddressFilter(BaseModel):
    """Case-insensitive substring filters for a registry address."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contact: FilterTerm | None = Field(default=None, description="Address contact name.")
    line1: FilterTerm | None = Field(default=None, description="First street-address line.")
    line2: FilterTerm | None = Field(default=None, description="Second street-address line.")
    line3: FilterTerm | None = Field(default=None, description="Third street-address line.")
    city: FilterTerm | None = Field(default=None, description="City or municipality.")
    province_state: FilterTerm | None = Field(
        default=None,
        description="Province, state, or its abbreviation.",
    )
    country: FilterTerm | None = Field(default=None, description="Country name or abbreviation.")
    postal_zip: FilterTerm | None = Field(
        default=None,
        description="Postal or ZIP code, including a partial prefix.",
    )

    @model_validator(mode="after")
    def _require_value(self) -> Self:
        if not any(value is not None for value in self.__dict__.values()):
            raise ValueError("provide at least one address field")
        return self


class LobbyistAddressFilter(BaseModel):
    """Case-insensitive filters for the address fields mirrored on lobbyist records."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    line1: FilterTerm | None = Field(default=None, description="First street-address line.")
    city: FilterTerm | None = Field(default=None, description="City or municipality.")
    province_state: FilterTerm | None = Field(
        default=None,
        description="Province, state, or its abbreviation.",
    )
    postal_zip: FilterTerm | None = Field(
        default=None,
        description="Postal or ZIP code, including a partial prefix.",
    )

    @model_validator(mode="after")
    def _require_value(self) -> Self:
        if not any(value is not None for value in self.__dict__.values()):
            raise ValueError("provide at least one address field")
        return self


class CompanySearchFilters(BaseModel):
    """Structured filters for company, condominium, and co-operative search.

    Different populated fields are combined with AND. Lists of registry values
    such as statuses are OR lists; text filters state their own any/all rule.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company_numbers: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Exact company numbers; any listed number may match.",
    )
    current_names: TextTermsFilter | None = Field(
        default=None,
        description="Current registered company-name substrings.",
    )
    corporation_types: list[CorporationType] | None = Field(
        default=None,
        min_length=1,
        max_length=3,
        description="Registry kinds; any listed kind may match.",
    )
    categories: list[Category] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
        description="Local or Extra-Provincial categories; any listed category may match.",
    )
    statuses: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Case-insensitive exact statuses; any listed status may match.",
        examples=[["Active"]],
    )
    business_types: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Case-insensitive exact business types; any listed value may match.",
    )
    incorporation_jurisdictions: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Case-insensitive exact incorporation jurisdictions.",
    )
    filing_types: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Case-insensitive exact filing types; any listed value may match.",
    )
    incorporation_date: DateRange | None = None
    registration_date: DateRange | None = None
    last_annual_return: DateRange | None = None
    director_names: TextTermsFilter | None = Field(
        default=None,
        description=(
            "Current director-name substrings. Use match='all' to require separate matches "
            "for every named person on the same company. Directors are not necessarily founders."
        ),
        examples=[{"terms": ["Jack Harrhy", "Martin Whelan"], "match": "all"}],
    )
    previous_names: TextTermsFilter | None = Field(
        default=None,
        description="Previously registered company-name substrings, when mirrored upstream data exists.",
    )
    historical_remarks: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in historical registry remarks.",
    )
    additional_info: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in the record's additional-information field.",
    )
    director_count_text: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in the upstream raw minimum/maximum directors text.",
    )
    registered_office: AddressFilter | None = None
    mailing_address: AddressFilter | None = None
    mailing_same_as_registered: bool | None = None
    has_current_directors: bool | None = None
    has_previous_names: bool | None = None
    has_historical_remarks: bool | None = None


class LobbyistSearchFilters(BaseModel):
    """Structured lobbyist filters; populated fields are combined with AND."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    registration_numbers: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Exact registration numbers; any listed number may match.",
    )
    lobbyist_types: list[LobbyistType] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
        description="Consultant or In-House; any listed type may match.",
    )
    statuses: list[FilterValue] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Case-insensitive exact registration statuses.",
    )
    registration_date: DateRange | None = None
    effective_date: DateRange | None = None
    amended_date: DateRange | None = None
    approval_date: DateRange | None = None
    contact_names: TextTermsFilter | None = Field(
        default=None,
        description="Named registration-contact substrings.",
    )
    firm_names: TextTermsFilter | None = Field(
        default=None,
        description="Lobbying firm or represented-organization name substrings.",
    )
    client_names: TextTermsFilter | None = Field(
        default=None,
        description="Consultant-lobbyist client-name substrings.",
    )
    in_house_lobbyist_names: TextTermsFilter | None = Field(
        default=None,
        description="Names in the registration's in-house lobbyist list.",
    )
    subject_matters: LobbyingActivityFilter | None = Field(
        default=None,
        description="Named subject matters and optional has/expected-to-lobby flags.",
    )
    lobbying_targets: LobbyingActivityFilter | None = Field(
        default=None,
        description="Named government targets and optional has/expected-to-lobby flags.",
    )
    communication_techniques: LobbyingActivityFilter | None = Field(
        default=None,
        description="Named communication techniques and optional has/expected-to-lobby flags.",
    )
    particulars: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in lobbying particulars.",
    )
    organization_description: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in the represented organization's description.",
    )
    organization_membership: TextTermsFilter | None = Field(
        default=None,
        description="Substrings in the represented organization's membership description.",
    )
    contact_address: LobbyistAddressFilter | None = None
    firm_address: LobbyistAddressFilter | None = None
    has_in_house_lobbyists: bool | None = None


class CompanyQueryMatch(BaseModel):
    """Why a free-text company query matched this result."""

    field: Literal["company_number", "current_name", "current_director", "previous_name"]
    value: str


class LobbyistQueryMatch(BaseModel):
    """Why a free-text lobbyist query matched this result."""

    field: Literal[
        "registration_number",
        "contact_name",
        "firm_name",
        "client_name",
        "in_house_lobbyist",
    ]
    value: str


class CompanySearchItem(BaseModel):
    """A compact company, condominium, or co-operative search result."""

    number: str
    name: str
    corporation_type: CorporationType
    status: str | None = None
    category: Category | None = None
    incorporation_date: date | None = None
    city: str | None = None
    province_state: str | None = None
    query_matches: list[CompanyQueryMatch] = Field(
        default_factory=list,
        description="Fields that matched the free-text query; empty when query was omitted.",
    )
    record_url: str


class CompanySearchPage(BaseModel):
    """A bounded page of company registry results."""

    total: int
    offset: int
    returned: int
    next_offset: int | None
    items: list[CompanySearchItem]


class LobbyistSearchItem(BaseModel):
    """A compact lobbyist registration search result."""

    registration_number: str
    contact_name: str | None = None
    firm_name: str | None = None
    lobbyist_type: str | None = None
    status: str | None = None
    effective_date: date | None = None
    query_matches: list[LobbyistQueryMatch] = Field(
        default_factory=list,
        description="Fields that matched the free-text query; empty when query was omitted.",
    )
    record_url: str


class LobbyistSearchPage(BaseModel):
    """A bounded page of lobbyist registry results."""

    total: int
    offset: int
    returned: int
    next_offset: int | None
    items: list[LobbyistSearchItem]


class CompanyRecord(Company):
    """A complete mirrored company record with provenance metadata."""

    ingested_at: datetime
    snapshot_id: str
    source_fetched_at: datetime
    snapshot_built_at: datetime
    published_at: datetime
    record_url: str
    source_registry_url: str = COMPANY_SOURCE_URL
    source_notice: str = SOURCE_NOTICE


class LobbyistRecord(LobbyistRegistration):
    """A complete mirrored lobbyist registration with provenance metadata."""

    ingested_at: datetime
    snapshot_id: str
    source_fetched_at: datetime
    snapshot_built_at: datetime
    published_at: datetime
    record_url: str
    source_registry_url: str = LOBBYIST_SOURCE_URL
    source_notice: str = SOURCE_NOTICE


class RegistrySnapshot(BaseModel):
    """Published record count for one registry."""

    count: int


class CompanyLocationOptions(BaseModel):
    """Distinct registered-office values offered by the company UI."""

    cities: list[str]
    provinces: list[str]


class DatasetStatus(BaseModel):
    """Coverage and freshness information for the local CADO mirror."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_name: str = "Government of Newfoundland and Labrador CADO"
    source_url: str = "https://cado.eservices.gov.nl.ca/"
    notice: str = SOURCE_NOTICE
    snapshot_id: str
    schema_version: int
    source_fetched_at: datetime
    snapshot_built_at: datetime
    published_at: datetime
    companies: RegistrySnapshot
    condominiums: RegistrySnapshot
    cooperatives: RegistrySnapshot
    lobbyists: RegistrySnapshot


class RegistryQueryService:
    """Execute bounded, read-only registry queries against a DuckDB file.

    A connection is scoped to each public operation.  This keeps calls made by
    FastAPI and MCP worker threads independent without maintaining a pool or a
    cross-thread global cursor.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self.db_path = db_path or settings.duckdb_path
        self.public_base_url = (public_base_url or settings.public_base_url).rstrip("/")

    @contextmanager
    def _connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = connect(self.db_path, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def search_companies(
        self,
        *,
        query: str = "",
        filters: CompanySearchFilters | None = None,
        sort_by: CompanySortField | None = None,
        sort_direction: SortDirection = "asc",
        limit: int = 20,
        offset: int = 0,
    ) -> CompanySearchPage:
        """Search company identities and apply structured registry filters."""
        _validate_page(limit, offset)
        filters = filters or CompanySearchFilters()
        clauses: list[str] = []
        params: list[object] = []
        term = query.strip()
        if term:
            pattern = _contains_pattern(term)
            clauses.append(
                """
                (
                    c.name ILIKE ? ESCAPE '\\'
                    OR lower(c.number) = lower(?)
                    OR EXISTS (
                        SELECT 1 FROM company_directors AS qd
                        WHERE qd.company_number = c.number
                          AND qd.full_name ILIKE ? ESCAPE '\\'
                    )
                    OR EXISTS (
                        SELECT 1 FROM company_previous_names AS qp
                        WHERE qp.company_number = c.number
                          AND qp.name ILIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            params.extend([pattern, term, pattern, pattern])

        _append_exact_filter(clauses, params, "c.number", filters.company_numbers)
        _append_text_filter(clauses, params, "c.name", filters.current_names)
        _append_exact_filter(clauses, params, "c.corporation_type", filters.corporation_types)
        _append_exact_filter(clauses, params, "c.category", filters.categories)
        _append_exact_filter(clauses, params, "c.status", filters.statuses)
        _append_exact_filter(clauses, params, "c.business_type", filters.business_types)
        _append_exact_filter(
            clauses,
            params,
            "c.incorporation_jurisdiction",
            filters.incorporation_jurisdictions,
        )
        _append_exact_filter(clauses, params, "c.filing_type", filters.filing_types)
        _append_date_range(clauses, params, "c.incorporation_date", filters.incorporation_date)
        _append_date_range(clauses, params, "c.registration_date", filters.registration_date)
        _append_date_range(clauses, params, "c.last_annual_return", filters.last_annual_return)
        _append_related_text_filter(
            clauses,
            params,
            relation="company_directors AS fd",
            relationship="fd.company_number = c.number",
            expression="fd.full_name",
            text_filter=filters.director_names,
        )
        _append_related_text_filter(
            clauses,
            params,
            relation="company_previous_names AS fp",
            relationship="fp.company_number = c.number",
            expression="fp.name",
            text_filter=filters.previous_names,
        )
        _append_related_text_filter(
            clauses,
            params,
            relation="company_historical_remarks AS fr",
            relationship="fr.company_number = c.number",
            expression="fr.remark",
            text_filter=filters.historical_remarks,
        )
        _append_text_filter(clauses, params, "c.additional_info", filters.additional_info)
        _append_text_filter(clauses, params, "c.min_max_directors", filters.director_count_text)
        _append_address_filter(clauses, params, "c", "ro", filters.registered_office)
        _append_address_filter(clauses, params, "c", "ma", filters.mailing_address)
        _append_boolean_filter(
            clauses,
            params,
            "c.ma_same_as_registered",
            filters.mailing_same_as_registered,
        )
        _append_exists_filter(
            clauses,
            "company_directors AS hd",
            "hd.company_number = c.number",
            filters.has_current_directors,
        )
        _append_exists_filter(
            clauses,
            "company_previous_names AS hp",
            "hp.company_number = c.number",
            filters.has_previous_names,
        )
        _append_exists_filter(
            clauses,
            "company_historical_remarks AS hr",
            "hr.company_number = c.number",
            filters.has_historical_remarks,
        )
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_by = _company_order_by(sort_by, sort_direction)

        evidence_sql = """
            NULL::VARCHAR AS query_number_match,
            NULL::VARCHAR AS query_name_match,
            NULL::VARCHAR AS query_director_match,
            NULL::VARCHAR AS query_previous_name_match
        """
        evidence_params: list[object] = []
        if term:
            pattern = _contains_pattern(term)
            evidence_sql = """
                CASE WHEN lower(c.number) = lower(?) THEN c.number END AS query_number_match,
                CASE WHEN c.name ILIKE ? ESCAPE '\\' THEN c.name END AS query_name_match,
                (
                    SELECT qd.full_name FROM company_directors AS qd
                    WHERE qd.company_number = c.number
                      AND qd.full_name ILIKE ? ESCAPE '\\'
                    ORDER BY qd.seq
                    LIMIT 1
                ) AS query_director_match,
                (
                    SELECT qp.name FROM company_previous_names AS qp
                    WHERE qp.company_number = c.number
                      AND qp.name ILIKE ? ESCAPE '\\'
                    ORDER BY qp.seq
                    LIMIT 1
                ) AS query_previous_name_match
            """
            evidence_params.extend([term, pattern, pattern, pattern])

        with self._connection() as conn:
            total = _scalar_int(
                conn.execute(f"SELECT COUNT(*) FROM companies AS c {where}", params).fetchone()
            )
            rows = conn.execute(
                f"""
                SELECT c.number, c.name, c.corporation_type, c.status, c.category,
                       c.incorporation_date, c.ro_city, c.ro_province_state,
                       {evidence_sql}
                FROM companies AS c
                {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                [*evidence_params, *params, limit, offset],
            ).fetchall()

        items = [
            CompanySearchItem(
                number=row[0],
                name=row[1],
                corporation_type=row[2],
                status=row[3],
                category=row[4],
                incorporation_date=row[5],
                city=row[6],
                province_state=row[7],
                query_matches=_company_query_matches(row[8:12]),
                record_url=self._record_url("company", row[0]),
            )
            for row in rows
        ]
        return CompanySearchPage(
            total=total,
            offset=offset,
            returned=len(items),
            next_offset=_next_offset(total, offset, len(items)),
            items=items,
        )

    def get_company_location_options(self) -> CompanyLocationOptions:
        """Return distinct registered-office cities and provinces for type-ahead inputs."""
        with self._connection() as conn:
            cities = conn.execute(
                """
                SELECT min(trim(ro_city)) AS value
                FROM companies
                WHERE NULLIF(trim(ro_city), '') IS NOT NULL
                GROUP BY lower(trim(ro_city))
                ORDER BY lower(value), value
                """
            ).fetchall()
            provinces = conn.execute(
                """
                SELECT min(trim(ro_province_state)) AS value
                FROM companies
                WHERE NULLIF(trim(ro_province_state), '') IS NOT NULL
                GROUP BY lower(trim(ro_province_state))
                ORDER BY lower(value), value
                """
            ).fetchall()
        return CompanyLocationOptions(
            cities=[row[0] for row in cities],
            provinces=[row[0] for row in provinces],
        )

    def get_company(self, number: str) -> CompanyRecord | None:
        """Return one complete company record, or ``None`` when absent."""
        key = number.strip()
        if not key:
            return None
        with self._connection() as conn:
            row = _fetch_dict(
                conn,
                "SELECT * FROM companies WHERE number = ?",
                [key],
            )
            if row is None:
                return None
            directors = conn.execute(
                """
                SELECT full_name, first_name, last_name
                FROM company_directors
                WHERE company_number = ?
                ORDER BY seq
                """,
                [key],
            ).fetchall()
            previous_names = conn.execute(
                """
                SELECT name, effective_date
                FROM company_previous_names
                WHERE company_number = ?
                ORDER BY seq
                """,
                [key],
            ).fetchall()
            remarks = conn.execute(
                """
                SELECT remark
                FROM company_historical_remarks
                WHERE company_number = ?
                ORDER BY seq
                """,
                [key],
            ).fetchall()
            provenance = _snapshot_provenance(conn)

        return CompanyRecord(
            number=row["number"],
            name=row["name"],
            corporation_type=row["corporation_type"],
            category=row["category"],
            status=row["status"],
            incorporation_date=row["incorporation_date"],
            registration_date=row["registration_date"],
            last_annual_return=row["last_annual_return"],
            business_type=row["business_type"],
            incorporation_jurisdiction=row["incorporation_jurisdiction"],
            filing_type=row["filing_type"],
            min_max_directors=row["min_max_directors"],
            additional_info=row["additional_info"],
            historical_remarks=[remark[0] for remark in remarks],
            registered_office=_company_address(row, "ro"),
            mailing_address=_company_address(row, "ma"),
            mailing_same_as_registered=row["ma_same_as_registered"],
            directors=[
                Director(full_name=item[0], first_name=item[1], last_name=item[2])
                for item in directors
            ],
            previous_names=[
                PreviousName(name=item[0], effective_date=item[1]) for item in previous_names
            ],
            ingested_at=row["ingested_at"],
            **provenance,
            record_url=self._record_url("company", row["number"]),
        )

    def search_lobbyists(
        self,
        *,
        query: str = "",
        filters: LobbyistSearchFilters | None = None,
        sort_by: LobbyistSortField | None = None,
        sort_direction: SortDirection = "asc",
        limit: int = 20,
        offset: int = 0,
    ) -> LobbyistSearchPage:
        """Search lobbyist identities and apply structured registry filters."""
        _validate_page(limit, offset)
        filters = filters or LobbyistSearchFilters()
        clauses: list[str] = []
        params: list[object] = []
        term = query.strip()
        if term:
            pattern = _contains_pattern(term)
            clauses.append(
                """
                (
                    lower(l.registration_number) = lower(?)
                    OR l.contact_name ILIKE ? ESCAPE '\\'
                    OR l.firm_name ILIKE ? ESCAPE '\\'
                    OR json_extract_string(l.raw_fields, '$.lblClientName') ILIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM json_each(l.in_house_lobbyists) AS qi
                        WHERE json_extract_string(qi.value, '$.name') ILIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            params.extend([term, pattern, pattern, pattern, pattern])

        _append_exact_filter(
            clauses,
            params,
            "l.registration_number",
            filters.registration_numbers,
        )
        _append_exact_filter(clauses, params, "l.lobbyist_type", filters.lobbyist_types)
        _append_exact_filter(clauses, params, "l.status", filters.statuses)
        _append_date_range(clauses, params, "l.registration_date", filters.registration_date)
        _append_date_range(clauses, params, "l.effective_date", filters.effective_date)
        _append_date_range(clauses, params, "l.amended_date", filters.amended_date)
        _append_date_range(clauses, params, "l.approval_date", filters.approval_date)
        _append_text_filter(clauses, params, "l.contact_name", filters.contact_names)
        _append_text_filter(clauses, params, "l.firm_name", filters.firm_names)
        _append_text_filter(
            clauses,
            params,
            "json_extract_string(l.raw_fields, '$.lblClientName')",
            filters.client_names,
        )
        _append_related_text_filter(
            clauses,
            params,
            relation="json_each(l.in_house_lobbyists) AS fi",
            relationship="TRUE",
            expression="json_extract_string(fi.value, '$.name')",
            text_filter=filters.in_house_lobbyist_names,
        )
        _append_activity_filter(
            clauses,
            params,
            "json_each(l.subject_matters) AS fs",
            "fs",
            filters.subject_matters,
        )
        _append_activity_filter(
            clauses,
            params,
            "json_each(l.lobbying_targets) AS ft",
            "ft",
            filters.lobbying_targets,
        )
        _append_activity_filter(
            clauses,
            params,
            "json_each(l.communication_techniques) AS fc",
            "fc",
            filters.communication_techniques,
        )
        _append_text_filter(clauses, params, "l.particulars", filters.particulars)
        _append_text_filter(
            clauses,
            params,
            "l.organization_description",
            filters.organization_description,
        )
        _append_text_filter(
            clauses,
            params,
            "l.organization_membership",
            filters.organization_membership,
        )
        _append_address_filter(clauses, params, "l", "contact", filters.contact_address)
        _append_address_filter(clauses, params, "l", "firm", filters.firm_address)
        _append_exists_filter(
            clauses,
            "json_each(l.in_house_lobbyists) AS hi",
            "TRUE",
            filters.has_in_house_lobbyists,
        )
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_by = _lobbyist_order_by(sort_by, sort_direction)

        evidence_sql = """
            NULL::VARCHAR AS query_number_match,
            NULL::VARCHAR AS query_contact_match,
            NULL::VARCHAR AS query_firm_match,
            NULL::VARCHAR AS query_client_match,
            NULL::VARCHAR AS query_in_house_match
        """
        evidence_params: list[object] = []
        if term:
            pattern = _contains_pattern(term)
            evidence_sql = """
                CASE WHEN lower(l.registration_number) = lower(?)
                     THEN l.registration_number END AS query_number_match,
                CASE WHEN l.contact_name ILIKE ? ESCAPE '\\'
                     THEN l.contact_name END AS query_contact_match,
                CASE WHEN l.firm_name ILIKE ? ESCAPE '\\'
                     THEN l.firm_name END AS query_firm_match,
                CASE WHEN json_extract_string(l.raw_fields, '$.lblClientName') ILIKE ? ESCAPE '\\'
                     THEN json_extract_string(l.raw_fields, '$.lblClientName')
                     END AS query_client_match,
                (
                    SELECT json_extract_string(qi.value, '$.name')
                    FROM json_each(l.in_house_lobbyists) AS qi
                    WHERE json_extract_string(qi.value, '$.name') ILIKE ? ESCAPE '\\'
                    LIMIT 1
                ) AS query_in_house_match
            """
            evidence_params.extend([term, pattern, pattern, pattern, pattern])

        with self._connection() as conn:
            total = _scalar_int(
                conn.execute(
                    f"SELECT COUNT(*) FROM lobbyist_registrations AS l {where}", params
                ).fetchone()
            )
            rows = conn.execute(
                f"""
                SELECT l.registration_number, l.contact_name, l.firm_name, l.lobbyist_type,
                       l.status, l.effective_date,
                       {evidence_sql}
                FROM lobbyist_registrations AS l
                {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                [*evidence_params, *params, limit, offset],
            ).fetchall()

        items = [
            LobbyistSearchItem(
                registration_number=row[0],
                contact_name=row[1],
                firm_name=row[2],
                lobbyist_type=row[3],
                status=row[4],
                effective_date=row[5],
                query_matches=_lobbyist_query_matches(row[6:11]),
                record_url=self._record_url("lobbyist", row[0]),
            )
            for row in rows
        ]
        return LobbyistSearchPage(
            total=total,
            offset=offset,
            returned=len(items),
            next_offset=_next_offset(total, offset, len(items)),
            items=items,
        )

    def get_lobbyist(self, registration_number: str) -> LobbyistRecord | None:
        """Return one complete lobbyist registration, or ``None`` when absent."""
        key = registration_number.strip()
        if not key:
            return None
        with self._connection() as conn:
            row = _fetch_dict(
                conn,
                "SELECT * FROM lobbyist_registrations WHERE registration_number = ?",
                [key],
            )
            provenance = _snapshot_provenance(conn)
        if row is None:
            return None

        raw_fields = row["raw_fields"]
        if isinstance(raw_fields, str):
            raw_fields = json.loads(raw_fields)
        if not isinstance(raw_fields, dict):
            raw_fields = {}
        return LobbyistRecord(
            registration_number=row["registration_number"],
            status=row["status"],
            lobbyist_type=row["lobbyist_type"],
            registration_date=row["registration_date"],
            effective_date=row["effective_date"],
            amended_date=row["amended_date"],
            approval_date=row["approval_date"],
            contact_name=row["contact_name"],
            contact_address=Address(
                line1=row["contact_line1"],
                city=row["contact_city"],
                province_state=row["contact_province_state"],
                postal_zip=row["contact_postal_zip"],
            ),
            firm_name=row["firm_name"],
            firm_address=Address(
                line1=row["firm_line1"],
                city=row["firm_city"],
                province_state=row["firm_province_state"],
                postal_zip=row["firm_postal_zip"],
            ),
            particulars=row["particulars"],
            organization_description=row["organization_description"],
            organization_membership=row["organization_membership"],
            subject_matters=[
                LobbyingActivity.model_validate(item) for item in _json_list(row["subject_matters"])
            ],
            lobbying_targets=[
                LobbyingActivity.model_validate(item)
                for item in _json_list(row["lobbying_targets"])
            ],
            communication_techniques=[
                LobbyingActivity.model_validate(item)
                for item in _json_list(row["communication_techniques"])
            ],
            in_house_lobbyists=[
                InHouseLobbyist.model_validate(item)
                for item in _json_list(row["in_house_lobbyists"])
            ],
            raw_fields=raw_fields,
            ingested_at=row["ingested_at"],
            **provenance,
            record_url=self._record_url("lobbyist", row["registration_number"]),
        )

    def get_dataset_status(self) -> DatasetStatus:
        """Return registry counts and truthful published-snapshot timestamps."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT corporation_type, COUNT(*)
                FROM companies
                GROUP BY corporation_type
                """
            ).fetchall()
            company_status = {row[0]: RegistrySnapshot(count=row[1]) for row in rows}
            lobbyist_row = conn.execute("SELECT COUNT(*) FROM lobbyist_registrations").fetchone()
            provenance = _snapshot_provenance(conn, include_schema=True)
        return DatasetStatus(
            **provenance,
            companies=company_status.get("Company", RegistrySnapshot(count=0)),
            condominiums=company_status.get("Condominium", RegistrySnapshot(count=0)),
            cooperatives=company_status.get("Co-operative", RegistrySnapshot(count=0)),
            lobbyists=RegistrySnapshot(count=_scalar_int(lobbyist_row)),
        )

    def _record_url(self, kind: str, key: str) -> str:
        return f"{self.public_base_url}/{kind}/{key}"


def _company_order_by(
    sort_by: CompanySortField | None,
    direction: SortDirection,
) -> str:
    return _sort_order(
        _COMPANY_SORT_EXPRESSIONS,
        sort_by,
        direction,
        default=("query_number_match IS NULL, query_name_match IS NULL, lower(c.name), c.number"),
        tie_breaker="lower(c.name), c.number",
    )


def _lobbyist_order_by(
    sort_by: LobbyistSortField | None,
    direction: SortDirection,
) -> str:
    return _sort_order(
        _LOBBYIST_SORT_EXPRESSIONS,
        sort_by,
        direction,
        default=(
            "query_number_match IS NULL, query_contact_match IS NULL, "
            "query_firm_match IS NULL, query_client_match IS NULL, "
            "l.effective_date DESC NULLS LAST, "
            "lower(coalesce(l.contact_name, '')), l.registration_number"
        ),
        tie_breaker="l.registration_number",
    )


def _sort_order(
    choices: dict[str, tuple[str, ...]],
    sort_by: str | None,
    direction: SortDirection,
    *,
    default: str,
    tie_breaker: str,
) -> str:
    """Build an ORDER BY fragment exclusively from trusted expressions."""
    if sort_by is None:
        return default
    expressions = choices.get(sort_by)
    if expressions is None:
        raise ValueError(f"unsupported sort field: {sort_by}")
    if direction not in ("asc", "desc"):
        raise ValueError(f"unsupported sort direction: {direction}")
    keyword = direction.upper()
    ordered = [f"{expression} {keyword} NULLS LAST" for expression in expressions]
    ordered.append(tie_breaker)
    return ", ".join(ordered)


def _contains_pattern(value: str) -> str:
    """Build a literal SQL LIKE substring pattern, escaping wildcard characters."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _append_exact_filter(
    clauses: list[str],
    params: list[object],
    expression: str,
    values: Sequence[object] | None,
) -> None:
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    clauses.append(f"lower({expression}) IN ({placeholders})")
    params.extend(str(value).strip().lower() for value in values)


def _append_text_filter(
    clauses: list[str],
    params: list[object],
    expression: str,
    text_filter: TextTermsFilter | None,
) -> None:
    if text_filter is None:
        return
    predicates = [f"{expression} ILIKE ? ESCAPE '\\'" for _ in text_filter.terms]
    operator = " OR " if text_filter.match is MatchMode.ANY else " AND "
    clauses.append(f"({operator.join(predicates)})")
    params.extend(_contains_pattern(term) for term in text_filter.terms)


def _append_related_text_filter(
    clauses: list[str],
    params: list[object],
    *,
    relation: str,
    relationship: str,
    expression: str,
    text_filter: TextTermsFilter | None,
) -> None:
    if text_filter is None:
        return
    if text_filter.match is MatchMode.ANY:
        predicates = [f"{expression} ILIKE ? ESCAPE '\\'" for _ in text_filter.terms]
        clauses.append(
            f"EXISTS (SELECT 1 FROM {relation} WHERE {relationship} AND "
            f"({' OR '.join(predicates)}))"
        )
        params.extend(_contains_pattern(term) for term in text_filter.terms)
        return
    for term in text_filter.terms:
        clauses.append(
            f"EXISTS (SELECT 1 FROM {relation} WHERE {relationship} "
            f"AND {expression} ILIKE ? ESCAPE '\\')"
        )
        params.append(_contains_pattern(term))


def _append_activity_filter(
    clauses: list[str],
    params: list[object],
    relation: str,
    alias: str,
    activity_filter: LobbyingActivityFilter | None,
) -> None:
    if activity_filter is None:
        return
    expression = f"json_extract_string({alias}.value, '$.name')"
    flags: list[tuple[str, bool]] = []
    if activity_filter.has_lobbied is not None:
        flags.append(("has_lobbied", activity_filter.has_lobbied))
    if activity_filter.expects_to_lobby is not None:
        flags.append(("expects_to_lobby", activity_filter.expects_to_lobby))

    def extra_conditions() -> tuple[str, list[object]]:
        conditions = [
            f"CAST(json_extract({alias}.value, '$.{field}') AS BOOLEAN) = ?" for field, _ in flags
        ]
        return "".join(f" AND {condition}" for condition in conditions), [
            value for _, value in flags
        ]

    if activity_filter.match is MatchMode.ANY:
        predicates = [f"{expression} ILIKE ? ESCAPE '\\'" for _ in activity_filter.terms]
        extras, extra_params = extra_conditions()
        clauses.append(
            f"EXISTS (SELECT 1 FROM {relation} WHERE ({' OR '.join(predicates)}){extras})"
        )
        params.extend(_contains_pattern(term) for term in activity_filter.terms)
        params.extend(extra_params)
        return
    for term in activity_filter.terms:
        extras, extra_params = extra_conditions()
        clauses.append(
            f"EXISTS (SELECT 1 FROM {relation} WHERE {expression} ILIKE ? ESCAPE '\\'{extras})"
        )
        params.append(_contains_pattern(term))
        params.extend(extra_params)


def _append_date_range(
    clauses: list[str],
    params: list[object],
    expression: str,
    date_range: DateRange | None,
) -> None:
    if date_range is None:
        return
    if date_range.date_from is not None:
        clauses.append(f"{expression} >= ?")
        params.append(date_range.date_from)
    if date_range.date_to is not None:
        clauses.append(f"{expression} <= ?")
        params.append(date_range.date_to)


def _append_address_filter(
    clauses: list[str],
    params: list[object],
    table_alias: str,
    column_prefix: str,
    address_filter: AddressFilter | LobbyistAddressFilter | None,
) -> None:
    if address_filter is None:
        return
    for field, value in address_filter.model_dump(exclude_none=True).items():
        clauses.append(f"{table_alias}.{column_prefix}_{field} ILIKE ? ESCAPE '\\'")
        params.append(_contains_pattern(str(value)))


def _append_boolean_filter(
    clauses: list[str],
    params: list[object],
    expression: str,
    value: bool | None,
) -> None:
    if value is None:
        return
    clauses.append(f"{expression} = ?")
    params.append(value)


def _append_exists_filter(
    clauses: list[str],
    relation: str,
    relationship: str,
    value: bool | None,
) -> None:
    if value is None:
        return
    prefix = "" if value else "NOT "
    clauses.append(f"{prefix}EXISTS (SELECT 1 FROM {relation} WHERE {relationship})")


def _company_query_matches(values: tuple[object, ...]) -> list[CompanyQueryMatch]:
    fields: tuple[
        Literal["company_number", "current_name", "current_director", "previous_name"], ...
    ] = ("company_number", "current_name", "current_director", "previous_name")
    return [
        CompanyQueryMatch(field=field, value=str(value))
        for field, value in zip(fields, values, strict=True)
        if value is not None
    ]


def _lobbyist_query_matches(values: tuple[object, ...]) -> list[LobbyistQueryMatch]:
    fields: tuple[
        Literal[
            "registration_number",
            "contact_name",
            "firm_name",
            "client_name",
            "in_house_lobbyist",
        ],
        ...,
    ] = (
        "registration_number",
        "contact_name",
        "firm_name",
        "client_name",
        "in_house_lobbyist",
    )
    return [
        LobbyistQueryMatch(field=field, value=str(value))
        for field, value in zip(fields, values, strict=True)
        if value is not None
    ]


def _snapshot_provenance(
    conn: duckdb.DuckDBPyConnection,
    *,
    include_schema: bool = False,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT schema_version, snapshot_id, source_fetched_at,
               snapshot_built_at, published_at
        FROM snapshot_metadata
        """
    ).fetchone()
    if row is None or row[4] is None:
        raise RuntimeError("DuckDB has no published snapshot metadata")
    provenance: dict[str, Any] = {
        "snapshot_id": row[1],
        "source_fetched_at": row[2],
        "snapshot_built_at": row[3],
        "published_at": row[4],
    }
    if include_schema:
        provenance["schema_version"] = row[0]
    return provenance


def _json_list(value: object) -> list[object]:
    if value is None:
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, list) else []


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if offset < 0:
        raise ValueError("offset must be zero or greater")


def _next_offset(total: int, offset: int, returned: int) -> int | None:
    candidate = offset + returned
    return candidate if returned and candidate < total else None


def _scalar_int(row: tuple[Any, ...] | None) -> int:
    if row is None:
        return 0
    return int(row[0])


def _fetch_dict(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[object],
) -> dict[str, Any] | None:
    result = conn.execute(sql, params)
    row = result.fetchone()
    if row is None:
        return None
    columns = [description[0] for description in result.description]
    return dict(zip(columns, row, strict=True))


def _company_address(row: dict[str, Any], prefix: str) -> Address:
    return Address(
        contact=row[f"{prefix}_contact"],
        line1=row[f"{prefix}_line1"],
        line2=row[f"{prefix}_line2"],
        line3=row[f"{prefix}_line3"],
        city=row[f"{prefix}_city"],
        province_state=row[f"{prefix}_province_state"],
        country=row[f"{prefix}_country"],
        postal_zip=row[f"{prefix}_postal_zip"],
    )
