"""DuckDB-backed storage layer."""

from .ingest import ingest_companies, ingest_lobbyists, ingest_one_html
from .session import connect, init_schema

__all__ = [
    "connect",
    "ingest_companies",
    "ingest_lobbyists",
    "ingest_one_html",
    "init_schema",
]
