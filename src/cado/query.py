"""Typed, read-only queries over the CADO DuckDB mirror.

This module is the application-facing boundary around DuckDB.  The HTML UI,
MCP server, and any future JSON API all consume the same response models and
query semantics instead of embedding SQL in their transport handlers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict

from .db import connect
from .models import (
    Address,
    Category,
    Company,
    CorporationType,
    Director,
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
    record_url: str
    source_registry_url: str = COMPANY_SOURCE_URL
    source_notice: str = SOURCE_NOTICE


class LobbyistRecord(LobbyistRegistration):
    """A complete mirrored lobbyist registration with provenance metadata."""

    ingested_at: datetime
    record_url: str
    source_registry_url: str = LOBBYIST_SOURCE_URL
    source_notice: str = SOURCE_NOTICE


class RegistrySnapshot(BaseModel):
    """Count and newest mirror timestamp for one registry."""

    count: int
    latest_ingested_at: datetime | None = None


class DatasetStatus(BaseModel):
    """Coverage and freshness information for the local CADO mirror."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_name: str = "Government of Newfoundland and Labrador CADO"
    source_url: str = "https://cado.eservices.gov.nl.ca/"
    notice: str = SOURCE_NOTICE
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
        corporation_type: CorporationType | str | None = None,
        status: str | None = None,
        category: Category | str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> CompanySearchPage:
        """Search companies, condominiums, and co-operatives in the mirror."""
        _validate_page(limit, offset)
        clauses: list[str] = []
        params: list[object] = []
        term = query.strip()
        if term:
            clauses.append("(name ILIKE ? OR number = ?)")
            params.extend([f"%{term}%", term])
        if corporation_type:
            clauses.append("corporation_type = ?")
            params.append(str(corporation_type))
        if status and status.strip():
            clauses.append("status ILIKE ?")
            params.append(status.strip())
        if category:
            clauses.append("category = ?")
            params.append(str(category))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._connection() as conn:
            total = _scalar_int(
                conn.execute(f"SELECT COUNT(*) FROM companies {where}", params).fetchone()
            )
            rows = conn.execute(
                f"""
                SELECT number, name, corporation_type, status, category,
                       incorporation_date, ro_city, ro_province_state
                FROM companies
                {where}
                ORDER BY lower(name), number
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
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
            record_url=self._record_url("company", row["number"]),
        )

    def search_lobbyists(
        self,
        *,
        query: str = "",
        lobbyist_type: LobbyistType | str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> LobbyistSearchPage:
        """Search lobbyist contacts, firms, and registration numbers."""
        _validate_page(limit, offset)
        clauses: list[str] = []
        params: list[object] = []
        term = query.strip()
        if term:
            clauses.append("(contact_name ILIKE ? OR firm_name ILIKE ? OR registration_number = ?)")
            params.extend([f"%{term}%", f"%{term}%", term])
        if lobbyist_type:
            clauses.append("lobbyist_type = ?")
            params.append(str(lobbyist_type))
        if status and status.strip():
            clauses.append("status ILIKE ?")
            params.append(status.strip())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._connection() as conn:
            total = _scalar_int(
                conn.execute(
                    f"SELECT COUNT(*) FROM lobbyist_registrations {where}", params
                ).fetchone()
            )
            rows = conn.execute(
                f"""
                SELECT registration_number, contact_name, firm_name, lobbyist_type,
                       status, effective_date
                FROM lobbyist_registrations
                {where}
                ORDER BY effective_date DESC NULLS LAST,
                         lower(coalesce(contact_name, '')),
                         registration_number
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

        items = [
            LobbyistSearchItem(
                registration_number=row[0],
                contact_name=row[1],
                firm_name=row[2],
                lobbyist_type=row[3],
                status=row[4],
                effective_date=row[5],
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
            raw_fields=raw_fields,
            ingested_at=row["ingested_at"],
            record_url=self._record_url("lobbyist", row["registration_number"]),
        )

    def get_dataset_status(self) -> DatasetStatus:
        """Return registry row counts and their newest ingestion timestamps."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT corporation_type, COUNT(*), MAX(ingested_at)
                FROM companies
                GROUP BY corporation_type
                """
            ).fetchall()
            company_status = {
                row[0]: RegistrySnapshot(count=row[1], latest_ingested_at=row[2]) for row in rows
            }
            lobbyist_row = conn.execute(
                "SELECT COUNT(*), MAX(ingested_at) FROM lobbyist_registrations"
            ).fetchone()
        return DatasetStatus(
            companies=company_status.get("Company", RegistrySnapshot(count=0)),
            condominiums=company_status.get("Condominium", RegistrySnapshot(count=0)),
            cooperatives=company_status.get("Co-operative", RegistrySnapshot(count=0)),
            lobbyists=RegistrySnapshot(
                count=_scalar_int(lobbyist_row),
                latest_ingested_at=lobbyist_row[1] if lobbyist_row else None,
            ),
        )

    def _record_url(self, kind: str, key: str) -> str:
        return f"{self.public_base_url}/{kind}/{key}"


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
