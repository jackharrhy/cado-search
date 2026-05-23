"""Re-parse cached HTML and upsert into DuckDB.

Ingest is *idempotent*: running it again over the same cache leaves the
database in the same state (rows are deleted+reinserted within a single
transaction per record). This means you can re-parse with a new parser
version without worrying about duplicates.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import duckdb

from ..parsers import parse_company_details, parse_lobbyist_detail
from ..parsers.company import CompanyParseError
from ..parsers.lobbyist import LobbyistParseError
from ..storage import HtmlCache
from .session import transaction

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestResult:
    """Per-record outcome of an ingest pass."""

    key: str
    parsed_ok: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


_COMPANY_INSERT: Final = """
INSERT OR REPLACE INTO companies (
    number, name, corporation_type, category, status,
    incorporation_date, registration_date, last_annual_return,
    business_type, incorporation_jurisdiction, filing_type, min_max_directors,
    additional_info,
    ro_contact, ro_line1, ro_line2, ro_line3, ro_city,
    ro_province_state, ro_country, ro_postal_zip,
    ma_contact, ma_line1, ma_line2, ma_line3, ma_city,
    ma_province_state, ma_country, ma_postal_zip, ma_same_as_registered,
    ingested_at, source_html_sha256
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?,
    ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    current_timestamp, ?
)
"""


def ingest_one_company(conn: duckdb.DuckDBPyConnection, key: str, html: str) -> IngestResult:
    """Parse one cached HTML and upsert into ``companies``-and-friends."""
    try:
        company = parse_company_details(html)
    except CompanyParseError as exc:
        return IngestResult(key=key, parsed_ok=False, error=str(exc))

    sha = _sha256(html)
    with transaction(conn):
        ro = company.registered_office
        ma = company.mailing_address
        conn.execute(
            _COMPANY_INSERT,
            [
                company.number,
                company.name,
                str(company.corporation_type),
                str(company.category) if company.category else None,
                company.status,
                company.incorporation_date,
                company.registration_date,
                company.last_annual_return,
                company.business_type,
                company.incorporation_jurisdiction,
                company.filing_type,
                company.min_max_directors,
                company.additional_info,
                ro.contact,
                ro.line1,
                ro.line2,
                ro.line3,
                ro.city,
                ro.province_state,
                ro.country,
                ro.postal_zip,
                ma.contact,
                ma.line1,
                ma.line2,
                ma.line3,
                ma.city,
                ma.province_state,
                ma.country,
                ma.postal_zip,
                company.mailing_same_as_registered,
                sha,
            ],
        )
        # Wipe + re-insert child tables. ``DELETE`` is cheap because of the PK.
        conn.execute(
            "DELETE FROM company_directors WHERE company_number = ?",
            [company.number],
        )
        for seq, director in enumerate(company.directors, start=1):
            conn.execute(
                "INSERT INTO company_directors VALUES (?, ?, ?, ?, ?)",
                [
                    company.number,
                    seq,
                    director.full_name,
                    director.first_name,
                    director.last_name,
                ],
            )
        conn.execute(
            "DELETE FROM company_previous_names WHERE company_number = ?",
            [company.number],
        )
        for seq, pname in enumerate(company.previous_names, start=1):
            conn.execute(
                "INSERT INTO company_previous_names VALUES (?, ?, ?, ?)",
                [company.number, seq, pname.name, pname.effective_date],
            )
        conn.execute(
            "DELETE FROM company_historical_remarks WHERE company_number = ?",
            [company.number],
        )
        for seq, remark in enumerate(company.historical_remarks, start=1):
            conn.execute(
                "INSERT INTO company_historical_remarks VALUES (?, ?, ?)",
                [company.number, seq, remark],
            )
    return IngestResult(key=key, parsed_ok=True)


def ingest_companies(conn: duckdb.DuckDBPyConnection, cache: HtmlCache) -> Iterator[IngestResult]:
    """Iterate every cached detail HTML, yielding ingest outcomes."""
    for key in cache.iter_keys(kind="detail"):
        html = cache.read(key, kind="detail")
        result = ingest_one_company(conn, key, html)
        _log_ingest(conn, "company", key, result, _sha256(html))
        yield result


# ---------------------------------------------------------------------------
# Lobbyists
# ---------------------------------------------------------------------------


_LOBBYIST_INSERT: Final = """
INSERT OR REPLACE INTO lobbyist_registrations (
    registration_number, lobbyist_type, status,
    registration_date, effective_date, amended_date, approval_date,
    contact_name, contact_line1, contact_city, contact_province_state, contact_postal_zip,
    firm_name, firm_line1, firm_city, firm_province_state, firm_postal_zip,
    particulars, organization_description, organization_membership,
    raw_fields,
    ingested_at, source_html_sha256
) VALUES (
    ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?::JSON,
    current_timestamp, ?
)
"""


def ingest_one_lobbyist(conn: duckdb.DuckDBPyConnection, key: str, html: str) -> IngestResult:
    try:
        reg = parse_lobbyist_detail(html)
    except LobbyistParseError as exc:
        return IngestResult(key=key, parsed_ok=False, error=str(exc))

    sha = _sha256(html)
    with transaction(conn):
        conn.execute(
            _LOBBYIST_INSERT,
            [
                reg.registration_number,
                reg.lobbyist_type,
                reg.status,
                reg.registration_date,
                reg.effective_date,
                reg.amended_date,
                reg.approval_date,
                reg.contact_name,
                reg.contact_address.line1,
                reg.contact_address.city,
                reg.contact_address.province_state,
                reg.contact_address.postal_zip,
                reg.firm_name,
                reg.firm_address.line1,
                reg.firm_address.city,
                reg.firm_address.province_state,
                reg.firm_address.postal_zip,
                reg.particulars,
                reg.organization_description,
                reg.organization_membership,
                json.dumps(reg.raw_fields, ensure_ascii=False),
                sha,
            ],
        )
    return IngestResult(key=key, parsed_ok=True)


def ingest_lobbyists(conn: duckdb.DuckDBPyConnection, cache: HtmlCache) -> Iterator[IngestResult]:
    for key in cache.iter_keys(kind="detail"):
        html = cache.read(key, kind="detail")
        result = ingest_one_lobbyist(conn, key, html)
        _log_ingest(conn, "lobbyist", key, result, _sha256(html))
        yield result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def ingest_one_html(
    conn: duckdb.DuckDBPyConnection, kind: str, key: str, html: str
) -> IngestResult:
    """Dispatch to the right ingest function by record kind."""
    if kind == "company":
        return ingest_one_company(conn, key, html)
    if kind == "lobbyist":
        return ingest_one_lobbyist(conn, key, html)
    raise ValueError(f"unknown ingest kind: {kind!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def _log_ingest(
    conn: duckdb.DuckDBPyConnection,
    kind: str,
    key: str,
    result: IngestResult,
    sha: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_log (kind, record_key, parsed_ok, error, source_html_sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        [kind, key, result.parsed_ok, result.error, sha],
    )
