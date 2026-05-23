"""DuckDB connection + schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import duckdb

from ..settings import settings


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply ``schema.sql`` to ``conn``. Idempotent (uses ``IF NOT EXISTS``)."""
    sql = resources.files("cado.db").joinpath("schema.sql").read_text("utf-8")
    # DuckDB's execute() runs a single statement; split on the standard
    # semicolon-newline boundary (our schema has no inline ``;`` literals).
    for statement in _split_statements(sql):
        conn.execute(statement)


def _split_statements(sql: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.strip().startswith("--") or not line.strip():
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


def connect(path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the project DuckDB, initialising the schema if needed."""
    db_path = path or settings.duckdb_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), read_only=read_only)
    if not read_only:
        init_schema(conn)
    return conn


@contextmanager
def transaction(conn: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """``BEGIN``..``COMMIT`` wrapper that rolls back on exception."""
    conn.execute("BEGIN TRANSACTION")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
