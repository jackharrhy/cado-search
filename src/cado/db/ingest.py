"""Re-parse cached HTML and upsert into DuckDB.

Ingest is *idempotent*: running it again over the same cache leaves the
database in the same state (rows are deleted+reinserted within a single
transaction per record). This means you can re-parse with a new parser
version without worrying about duplicates.

There are two code paths:

* ``ingest_one_company`` / ``ingest_one_lobbyist`` -- row-by-row INSERTs.
  Used by the dispatcher ``ingest_one_html`` for one-off / test ingestion.

* ``ingest_companies`` / ``ingest_lobbyists`` -- the bulk path. Parsed
  records are buffered in memory, then flushed in batches via temporary
  CSV files (DuckDB's ``read_csv`` is ~150x faster than ``executemany``).
  This is what the ``cado ingest`` CLI uses for the full registry.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import duckdb

from ..models import Company, LobbyistRegistration
from ..parsers import parse_company_details, parse_lobbyist_detail
from ..parsers.company import CompanyParseError
from ..parsers.lobbyist import LobbyistParseError
from ..storage import HtmlCache
from .session import transaction

log = logging.getLogger(__name__)


# Tuned for "small enough to keep many batches' worth of rows in memory, but
# large enough that DuckDB's CSV scanner amortises its overhead". 5000 records
# = ~5 MB of buffered CSV at most, which is trivial.
_DEFAULT_BATCH_SIZE: Final = 5000


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
# Single-record (row-by-row) ingest -- kept for one-off / test use
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
        conn.execute(_COMPANY_INSERT, [*_company_parent_row(company), sha])
        for tbl, col, rows in _company_child_rows(company):
            conn.execute(f"DELETE FROM {tbl} WHERE {col} = ?", [company.number])
            for row in rows:
                placeholders = ",".join(["?"] * len(row))
                conn.execute(f"INSERT INTO {tbl} VALUES ({placeholders})", row)
    return IngestResult(key=key, parsed_ok=True)


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
        conn.execute(_LOBBYIST_INSERT, [*_lobbyist_row(reg), sha])
    return IngestResult(key=key, parsed_ok=True)


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
# Bulk path: companies
# ---------------------------------------------------------------------------

_COMPANY_COLUMNS: Final = [
    "number",
    "name",
    "corporation_type",
    "category",
    "status",
    "incorporation_date",
    "registration_date",
    "last_annual_return",
    "business_type",
    "incorporation_jurisdiction",
    "filing_type",
    "min_max_directors",
    "additional_info",
    "ro_contact",
    "ro_line1",
    "ro_line2",
    "ro_line3",
    "ro_city",
    "ro_province_state",
    "ro_country",
    "ro_postal_zip",
    "ma_contact",
    "ma_line1",
    "ma_line2",
    "ma_line3",
    "ma_city",
    "ma_province_state",
    "ma_country",
    "ma_postal_zip",
    "ma_same_as_registered",
    "source_html_sha256",
]
_DIRECTOR_COLUMNS: Final = ["company_number", "seq", "full_name", "first_name", "last_name"]
_PREV_NAME_COLUMNS: Final = ["company_number", "seq", "name", "effective_date"]
_REMARK_COLUMNS: Final = ["company_number", "seq", "remark"]


def ingest_companies(
    conn: duckdb.DuckDBPyConnection,
    cache: HtmlCache,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> Iterator[IngestResult]:
    """Iterate every cached detail HTML, bulk-loading via temp CSV batches."""
    buf = _CompanyBuffer(conn, batch_size=batch_size)
    log_buf = _IngestLogBuffer(conn, "company", batch_size=batch_size)
    for key in cache.iter_keys(kind="detail"):
        html = cache.read(key, kind="detail")
        sha = _sha256(html)
        try:
            company = parse_company_details(html)
        except CompanyParseError as exc:
            result = IngestResult(key=key, parsed_ok=False, error=str(exc))
            log_buf.add(key, result, sha)
            yield result
            continue
        buf.add(company, sha)
        result = IngestResult(key=key, parsed_ok=True)
        log_buf.add(key, result, sha)
        yield result
    buf.flush()
    log_buf.flush()


class _CompanyBuffer:
    """In-memory batch of parsed companies; flushes to DuckDB via temp CSV.

    A single flush:

    1. Writes the batch's parent and child rows to four temp CSV files.
    2. ``DELETE``s any pre-existing rows for the batch's company numbers
       (one ``IN`` clause per table -- cheap when the batch is bounded).
    3. ``INSERT INTO ... SELECT * FROM read_csv(...)`` for each table.

    Empirically ~150x faster than per-row ``INSERT`` statements.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, *, batch_size: int) -> None:
        self.conn = conn
        self.batch_size = batch_size
        self.parents: list[list[object]] = []
        self.directors: list[list[object]] = []
        self.previous_names: list[list[object]] = []
        self.remarks: list[list[object]] = []
        self.parent_keys: list[str] = []

    def add(self, company: Company, sha: str) -> None:
        self.parents.append([*_company_parent_row(company), sha])
        self.parent_keys.append(company.number)
        for tbl, _col, rows in _company_child_rows(company):
            target = {
                "company_directors": self.directors,
                "company_previous_names": self.previous_names,
                "company_historical_remarks": self.remarks,
            }[tbl]
            target.extend(rows)
        if len(self.parents) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.parents:
            return
        with tempfile.TemporaryDirectory(prefix="cado-ingest-") as td:
            tdpath = Path(td)
            with transaction(self.conn):
                self._delete_existing()
                self._copy_in(tdpath, "companies", _COMPANY_COLUMNS, self.parents)
                self._copy_in(tdpath, "company_directors", _DIRECTOR_COLUMNS, self.directors)
                self._copy_in(
                    tdpath,
                    "company_previous_names",
                    _PREV_NAME_COLUMNS,
                    self.previous_names,
                )
                self._copy_in(
                    tdpath,
                    "company_historical_remarks",
                    _REMARK_COLUMNS,
                    self.remarks,
                )
        self.parents.clear()
        self.directors.clear()
        self.previous_names.clear()
        self.remarks.clear()
        self.parent_keys.clear()

    def _delete_existing(self) -> None:
        # Push the batch's keys through a temp table so DELETE ... IN (...)
        # doesn't have to materialise a long parameter list.
        self.conn.execute("CREATE OR REPLACE TEMP TABLE _ingest_keys (k VARCHAR)")
        self.conn.executemany(
            "INSERT INTO _ingest_keys VALUES (?)",
            [[k] for k in self.parent_keys],
        )
        for tbl, col in [
            ("companies", "number"),
            ("company_directors", "company_number"),
            ("company_previous_names", "company_number"),
            ("company_historical_remarks", "company_number"),
        ]:
            self.conn.execute(f"DELETE FROM {tbl} WHERE {col} IN (SELECT k FROM _ingest_keys)")
        self.conn.execute("DROP TABLE _ingest_keys")

    def _copy_in(
        self,
        tdpath: Path,
        table: str,
        columns: list[str],
        rows: list[list[object]],
    ) -> None:
        if not rows:
            return
        csv_path = tdpath / f"{table}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(_serialise_csv_row(row))
        # Explicit columns so DuckDB's CSV scanner doesn't have to sniff types
        # over the whole file. ``ingested_at`` is filled in by the SELECT.
        col_list = ", ".join(columns)
        read_call = _read_csv_call(csv_path, columns, table)
        if table == "companies":
            self.conn.execute(
                f"INSERT INTO {table} ({col_list}, ingested_at) "
                f"SELECT {col_list}, current_timestamp AS ingested_at "
                f"FROM {read_call}"
            )
        else:
            self.conn.execute(
                f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {read_call}"
            )


# ---------------------------------------------------------------------------
# Bulk path: ingest_log buffer
# ---------------------------------------------------------------------------


class _IngestLogBuffer:
    """Batches ``ingest_log`` inserts into temp CSV flushes.

    The previous implementation called ``conn.execute("INSERT ...", [...])``
    per record. Profiling showed that path triggers a DuckDB-side check that
    imports pandas (for replacement-scan autodetection of values), adding
    ~10ms of overhead *per row*. Going through a temp-CSV bulk insert avoids
    that path entirely.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        kind: str,
        *,
        batch_size: int,
    ) -> None:
        self.conn = conn
        self.kind = kind
        self.batch_size = batch_size
        self.rows: list[list[object]] = []

    def add(self, key: str, result: IngestResult, sha: str) -> None:
        self.rows.append([self.kind, key, result.parsed_ok, result.error, sha])
        if len(self.rows) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        with tempfile.TemporaryDirectory(prefix="cado-ingest-log-") as td:
            csv_path = Path(td) / "log.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(_INGEST_LOG_COLUMNS)
                for row in self.rows:
                    writer.writerow(_serialise_csv_row(row))
            col_list = ", ".join(_INGEST_LOG_COLUMNS)
            read_call = _read_csv_call(csv_path, _INGEST_LOG_COLUMNS, "ingest_log")
            self.conn.execute(
                f"INSERT INTO ingest_log ({col_list}) SELECT {col_list} FROM {read_call}"
            )
        self.rows.clear()


# ---------------------------------------------------------------------------
# Bulk path: lobbyists
# ---------------------------------------------------------------------------

_LOBBYIST_COLUMNS: Final = [
    "registration_number",
    "lobbyist_type",
    "status",
    "registration_date",
    "effective_date",
    "amended_date",
    "approval_date",
    "contact_name",
    "contact_line1",
    "contact_city",
    "contact_province_state",
    "contact_postal_zip",
    "firm_name",
    "firm_line1",
    "firm_city",
    "firm_province_state",
    "firm_postal_zip",
    "particulars",
    "organization_description",
    "organization_membership",
    "raw_fields",
    "source_html_sha256",
]


def ingest_lobbyists(
    conn: duckdb.DuckDBPyConnection,
    cache: HtmlCache,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> Iterator[IngestResult]:
    buf: list[list[object]] = []
    keys: list[str] = []
    log_buf = _IngestLogBuffer(conn, "lobbyist", batch_size=batch_size)

    def flush() -> None:
        if not buf:
            return
        with tempfile.TemporaryDirectory(prefix="cado-ingest-") as td:
            csv_path = Path(td) / "lobbyists.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(_LOBBYIST_COLUMNS)
                for row in buf:
                    writer.writerow(_serialise_csv_row(row))
            with transaction(conn):
                # DELETE existing keys, then INSERT.
                conn.execute("CREATE OR REPLACE TEMP TABLE _ingest_keys (k VARCHAR)")
                conn.executemany("INSERT INTO _ingest_keys VALUES (?)", [[k] for k in keys])
                conn.execute(
                    "DELETE FROM lobbyist_registrations "
                    "WHERE registration_number IN (SELECT k FROM _ingest_keys)"
                )
                conn.execute("DROP TABLE _ingest_keys")
                col_list = ", ".join(_LOBBYIST_COLUMNS)
                read_call = _read_csv_call(csv_path, _LOBBYIST_COLUMNS, "lobbyist_registrations")
                conn.execute(
                    f"INSERT INTO lobbyist_registrations ({col_list}, ingested_at) "
                    f"SELECT {col_list}, current_timestamp FROM {read_call}"
                )
        buf.clear()
        keys.clear()

    for key in cache.iter_keys(kind="detail"):
        html = cache.read(key, kind="detail")
        sha = _sha256(html)
        try:
            reg = parse_lobbyist_detail(html)
        except LobbyistParseError as exc:
            result = IngestResult(key=key, parsed_ok=False, error=str(exc))
            log_buf.add(key, result, sha)
            yield result
            continue
        buf.append([*_lobbyist_row(reg), sha])
        keys.append(reg.registration_number)
        result = IngestResult(key=key, parsed_ok=True)
        log_buf.add(key, result, sha)
        yield result
        if len(buf) >= batch_size:
            flush()
    flush()
    log_buf.flush()


# ---------------------------------------------------------------------------
# Row builders -- shared by the single-record and bulk paths
# ---------------------------------------------------------------------------


def _company_parent_row(company: Company) -> list[object]:
    """Build the ordered parent row for the ``companies`` table."""
    ro = company.registered_office
    ma = company.mailing_address
    return [
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
    ]


def _company_child_rows(
    company: Company,
) -> list[tuple[str, str, list[list[object]]]]:
    """Build the child rows keyed by ``(table, fk_column, rows)``."""
    return [
        (
            "company_directors",
            "company_number",
            [
                [company.number, seq, d.full_name, d.first_name, d.last_name]
                for seq, d in enumerate(company.directors, start=1)
            ],
        ),
        (
            "company_previous_names",
            "company_number",
            [
                [company.number, seq, p.name, p.effective_date]
                for seq, p in enumerate(company.previous_names, start=1)
            ],
        ),
        (
            "company_historical_remarks",
            "company_number",
            [[company.number, seq, r] for seq, r in enumerate(company.historical_remarks, start=1)],
        ),
    ]


def _lobbyist_row(reg: LobbyistRegistration) -> list[object]:
    return [
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
    ]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _serialise_csv_row(row: list[object]) -> list[str]:
    """Convert a row of typed values to CSV-friendly strings.

    Empty strings are written as ``""``; DuckDB's CSV scanner treats those as
    ``NULL`` when ``columns=`` specifies the type, which is exactly what we
    want. Dates use ISO-8601, booleans use ``true`` / ``false``.
    """
    out: list[str] = []
    for v in row:
        if v is None:
            out.append("")
        elif isinstance(v, bool):
            out.append("true" if v else "false")
        elif isinstance(v, date):
            out.append(v.isoformat())
        else:
            out.append(str(v))
    return out


def _csv_columns_decl(columns: list[str], table: str) -> str:
    """Render the ``columns={'name': 'TYPE', ...}`` map for ``read_csv``.

    DuckDB scans CSVs much faster when given an explicit column->type map
    than when it has to auto-detect. We hard-code the types here per table
    so they stay in sync with ``schema.sql``.
    """
    types = _COLUMN_TYPES[table]
    pairs = ", ".join(f"'{c}': '{types[c]}'" for c in columns)
    return "{" + pairs + "}"


def _read_csv_call(csv_path: Path, columns: list[str], table: str) -> str:
    """Render a ``read_csv(...)`` call with the right options for our writer.

    Critical: we set ``quote`` and ``escape`` explicitly to ``"``. Without
    that, DuckDB's auto-detection samples the first 20KB; if most rows in
    that window are unquoted (which they are -- ``csv.writer`` only quotes
    fields containing the delimiter, quote char, or newline), it concludes
    that ``escape`` is empty. When a later row contains ``""`` as an escaped
    quote, the parser treats it as 'close quote, open quote' and fails with
    'unterminated quote' on a perfectly valid RFC 4180 row.

    Python's ``csv.writer`` is RFC 4180 by default: quote when needed,
    escape internal quotes by doubling them. We mirror that on the read
    side with ``quote='"', escape='"'``.
    """
    return (
        f"read_csv("
        f"'{csv_path}', "
        f"header=true, "
        f"quote='\"', escape='\"', "
        f"columns={_csv_columns_decl(columns, table)}"
        f")"
    )


# Per-table column -> SQL type. Stays in sync with ``schema.sql``.
_COLUMN_TYPES: Final[dict[str, dict[str, str]]] = {
    "companies": {
        "number": "VARCHAR",
        "name": "VARCHAR",
        "corporation_type": "VARCHAR",
        "category": "VARCHAR",
        "status": "VARCHAR",
        "incorporation_date": "DATE",
        "registration_date": "DATE",
        "last_annual_return": "DATE",
        "business_type": "VARCHAR",
        "incorporation_jurisdiction": "VARCHAR",
        "filing_type": "VARCHAR",
        "min_max_directors": "VARCHAR",
        "additional_info": "VARCHAR",
        "ro_contact": "VARCHAR",
        "ro_line1": "VARCHAR",
        "ro_line2": "VARCHAR",
        "ro_line3": "VARCHAR",
        "ro_city": "VARCHAR",
        "ro_province_state": "VARCHAR",
        "ro_country": "VARCHAR",
        "ro_postal_zip": "VARCHAR",
        "ma_contact": "VARCHAR",
        "ma_line1": "VARCHAR",
        "ma_line2": "VARCHAR",
        "ma_line3": "VARCHAR",
        "ma_city": "VARCHAR",
        "ma_province_state": "VARCHAR",
        "ma_country": "VARCHAR",
        "ma_postal_zip": "VARCHAR",
        "ma_same_as_registered": "BOOLEAN",
        "source_html_sha256": "VARCHAR",
    },
    "company_directors": {
        "company_number": "VARCHAR",
        "seq": "INTEGER",
        "full_name": "VARCHAR",
        "first_name": "VARCHAR",
        "last_name": "VARCHAR",
    },
    "company_previous_names": {
        "company_number": "VARCHAR",
        "seq": "INTEGER",
        "name": "VARCHAR",
        "effective_date": "DATE",
    },
    "company_historical_remarks": {
        "company_number": "VARCHAR",
        "seq": "INTEGER",
        "remark": "VARCHAR",
    },
    "lobbyist_registrations": {
        "registration_number": "VARCHAR",
        "lobbyist_type": "VARCHAR",
        "status": "VARCHAR",
        "registration_date": "DATE",
        "effective_date": "DATE",
        "amended_date": "DATE",
        "approval_date": "DATE",
        "contact_name": "VARCHAR",
        "contact_line1": "VARCHAR",
        "contact_city": "VARCHAR",
        "contact_province_state": "VARCHAR",
        "contact_postal_zip": "VARCHAR",
        "firm_name": "VARCHAR",
        "firm_line1": "VARCHAR",
        "firm_city": "VARCHAR",
        "firm_province_state": "VARCHAR",
        "firm_postal_zip": "VARCHAR",
        "particulars": "VARCHAR",
        "organization_description": "VARCHAR",
        "organization_membership": "VARCHAR",
        "raw_fields": "JSON",
        "source_html_sha256": "VARCHAR",
    },
    "ingest_log": {
        "kind": "VARCHAR",
        "record_key": "VARCHAR",
        "parsed_ok": "BOOLEAN",
        "error": "VARCHAR",
        "source_html_sha256": "VARCHAR",
    },
}


_INGEST_LOG_COLUMNS: Final = [
    "kind",
    "record_key",
    "parsed_ok",
    "error",
    "source_html_sha256",
]


# ---------------------------------------------------------------------------
# Internal: misc
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
