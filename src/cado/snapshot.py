"""Build, validate, and atomically publish immutable registry snapshots."""

from __future__ import annotations

import fcntl
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
from pydantic import BaseModel, ConfigDict

from .db import connect, ingest_companies, ingest_lobbyists
from .storage import HtmlCache, replace_bytes

SCHEMA_VERSION = 2
REQUIRED_TABLES = {
    "companies",
    "company_directors",
    "company_previous_names",
    "company_historical_remarks",
    "lobbyist_registrations",
    "ingest_log",
    "snapshot_metadata",
}


class SnapshotError(RuntimeError):
    """Base exception for snapshot lifecycle failures."""


class SnapshotValidationError(SnapshotError):
    """Raised when a candidate snapshot is unsafe to publish."""


class SnapshotManifest(BaseModel):
    """The small amount of state needed to resume an upstream fetch."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    fetch_started_at: datetime
    source_fetched_at: datetime | None = None
    company_start: int
    company_stop: int
    company_fetch_complete: bool = False
    lobbyist_fetch_complete: bool = False
    lobbyist_expected_count: int | None = None

    @classmethod
    def create(cls, *, company_start: int, company_stop: int) -> SnapshotManifest:
        now = datetime.now(UTC)
        return cls(
            snapshot_id=f"{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}",
            fetch_started_at=now,
            company_start=company_start,
            company_stop=company_stop,
        )


@dataclass(slots=True, frozen=True)
class SnapshotWorkspace:
    """Paths and manifest for the single resumable refresh workspace."""

    root: Path
    manifest: SnapshotManifest

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def html_root(self) -> Path:
        return self.root / "html"

    @property
    def candidate_db_path(self) -> Path:
        return self.root / "cado.next.duckdb"

    def company_cache(self) -> HtmlCache:
        return HtmlCache(root=self.html_root, registry="companies")

    def lobbyist_cache(self) -> HtmlCache:
        return HtmlCache(root=self.html_root, registry="lobbyists")

    def save(self) -> None:
        replace_bytes(
            self.manifest_path,
            (self.manifest.model_dump_json(indent=2) + "\n").encode(),
        )


@dataclass(slots=True, frozen=True)
class SnapshotReport:
    snapshot_id: str
    company_count: int
    condominium_count: int
    cooperative_count: int
    lobbyist_count: int
    company_cache_count: int
    lobbyist_cache_count: int
    snapshot_built_at: datetime


@dataclass(slots=True, frozen=True)
class SnapshotMetadata:
    """Provenance read from a built or published DuckDB snapshot."""

    schema_version: int
    snapshot_id: str
    fetch_started_at: datetime
    source_fetched_at: datetime
    snapshot_built_at: datetime
    published_at: datetime | None
    company_start: int
    company_stop: int
    lobbyist_expected_count: int | None
    company_cache_count: int
    lobbyist_cache_count: int


@dataclass(slots=True, frozen=True)
class _CandidateContents:
    by_company_type: dict[str, int]
    lobbyist_count: int
    built_at: datetime


def open_workspace(
    data_dir: Path,
    *,
    company_start: int,
    company_stop: int,
) -> SnapshotWorkspace:
    """Create or resume the one in-progress full refresh."""
    if company_start < 1 or company_stop <= company_start:
        raise SnapshotError("company range must satisfy 1 <= start < stop")
    root = data_dir / "refresh"
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = SnapshotManifest.model_validate_json(manifest_path.read_text())
        if manifest.company_start != company_start or company_stop < manifest.company_stop:
            raise SnapshotError(
                "an unfinished refresh uses company range "
                f"[{manifest.company_start}, {manifest.company_stop}); resume with that "
                "start and an equal or larger stop, or run `cado clean refresh --yes`"
            )
        if company_stop > manifest.company_stop:
            if manifest.company_fetch_complete:
                raise SnapshotError(
                    "the completed company fetch cannot be extended; start a new refresh"
                )
            manifest.company_stop = company_stop
            workspace = SnapshotWorkspace(root=root, manifest=manifest)
            workspace.save()
            return workspace
        return SnapshotWorkspace(root=root, manifest=manifest)
    elif root.exists() and any(root.iterdir()):
        raise SnapshotError(
            f"refresh workspace {root} has data but no valid manifest; inspect it and run "
            "`cado clean refresh --yes` before retrying"
        )

    root.mkdir(parents=True, exist_ok=True)
    workspace = SnapshotWorkspace(
        root=root,
        manifest=SnapshotManifest.create(
            company_start=company_start,
            company_stop=company_stop,
        ),
    )
    workspace.save()
    return workspace


@contextmanager
def refresh_lock(data_dir: Path) -> Iterator[None]:
    """Prevent overlapping refresh processes on the same data volume."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".refresh.lock"
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError(f"another refresh already holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_snapshot(
    workspace: SnapshotWorkspace,
    *,
    live_db_path: Path,
    max_count_drop_fraction: float = 0.20,
) -> SnapshotReport:
    """Rebuild a candidate DuckDB solely from the staged raw HTML."""
    manifest = workspace.manifest
    if not manifest.company_fetch_complete or not manifest.lobbyist_fetch_complete:
        raise SnapshotValidationError("both registry fetches must complete before snapshot build")
    if manifest.source_fetched_at is None:
        raise SnapshotValidationError("completed fetch is missing source_fetched_at")
    if not 0 <= max_count_drop_fraction < 1:
        raise ValueError("max_count_drop_fraction must be in [0, 1)")

    candidate = workspace.candidate_db_path
    candidate.unlink(missing_ok=True)
    candidate.with_suffix(".duckdb.wal").unlink(missing_ok=True)
    company_cache = workspace.company_cache()
    lobbyist_cache = workspace.lobbyist_cache()
    company_cache_count = sum(1 for _ in company_cache.iter_keys(kind="detail"))
    lobbyist_cache_count = sum(1 for _ in lobbyist_cache.iter_keys(kind="detail"))
    if company_cache_count == 0 or lobbyist_cache_count == 0:
        raise SnapshotValidationError("candidate raw snapshot has an empty registry cache")

    conn = connect(candidate)
    try:
        contents = _populate_candidate(
            conn,
            workspace=workspace,
            company_cache=company_cache,
            lobbyist_cache=lobbyist_cache,
            company_cache_count=company_cache_count,
            lobbyist_cache_count=lobbyist_cache_count,
            live_db_path=live_db_path,
            max_count_drop_fraction=max_count_drop_fraction,
        )
    except BaseException:
        conn.close()
        candidate.unlink(missing_ok=True)
        candidate.with_suffix(".duckdb.wal").unlink(missing_ok=True)
        raise
    else:
        conn.close()

    return SnapshotReport(
        snapshot_id=manifest.snapshot_id,
        company_count=contents.by_company_type.get("Company", 0),
        condominium_count=contents.by_company_type.get("Condominium", 0),
        cooperative_count=contents.by_company_type.get("Co-operative", 0),
        lobbyist_count=contents.lobbyist_count,
        company_cache_count=company_cache_count,
        lobbyist_cache_count=lobbyist_cache_count,
        snapshot_built_at=contents.built_at,
    )


def _populate_candidate(
    conn: duckdb.DuckDBPyConnection,
    *,
    workspace: SnapshotWorkspace,
    company_cache: HtmlCache,
    lobbyist_cache: HtmlCache,
    company_cache_count: int,
    lobbyist_cache_count: int,
    live_db_path: Path,
    max_count_drop_fraction: float,
) -> _CandidateContents:
    failures = [result for result in ingest_companies(conn, company_cache) if not result.parsed_ok]
    failures.extend(
        result for result in ingest_lobbyists(conn, lobbyist_cache) if not result.parsed_ok
    )
    if failures:
        sample = "; ".join(f"{item.key}: {item.error}" for item in failures[:5])
        raise SnapshotValidationError(
            f"candidate contains {len(failures)} parse error(s); first errors: {sample}"
        )
    rows = conn.execute(
        "SELECT corporation_type, COUNT(*) FROM companies GROUP BY corporation_type"
    ).fetchall()
    by_type = {str(row[0]): int(row[1]) for row in rows}
    company_count = sum(by_type.values())
    lobbyist_count = _scalar_count(
        conn.execute("SELECT COUNT(*) FROM lobbyist_registrations").fetchone()
    )
    if company_count != company_cache_count:
        raise SnapshotValidationError(
            f"parsed company count {company_count} does not match raw cache count {company_cache_count}"
        )
    if lobbyist_count != lobbyist_cache_count:
        raise SnapshotValidationError(
            f"parsed lobbyist count {lobbyist_count} does not match raw cache "
            f"count {lobbyist_cache_count}"
        )
    expected = workspace.manifest.lobbyist_expected_count
    if expected is not None and lobbyist_count != expected:
        raise SnapshotValidationError(
            f"lobbyist count {lobbyist_count} does not match upstream total {expected}"
        )
    _validate_count_drop(
        live_db_path,
        new_company_count=company_count,
        new_lobbyist_count=lobbyist_count,
        max_drop_fraction=max_count_drop_fraction,
    )
    built_at = datetime.now(UTC)
    _write_snapshot_metadata(
        conn,
        manifest=workspace.manifest,
        built_at=built_at,
        company_cache_count=company_cache_count,
        lobbyist_cache_count=lobbyist_cache_count,
    )
    conn.execute("CHECKPOINT")
    return _CandidateContents(by_type, lobbyist_count, built_at)


def _write_snapshot_metadata(
    conn: duckdb.DuckDBPyConnection,
    *,
    manifest: SnapshotManifest,
    built_at: datetime,
    company_cache_count: int,
    lobbyist_cache_count: int,
) -> None:
    conn.execute("DELETE FROM snapshot_metadata")
    conn.execute(
        """
        INSERT INTO snapshot_metadata (
            singleton, schema_version, snapshot_id, fetch_started_at,
            source_fetched_at, snapshot_built_at, published_at,
            company_start, company_stop, lobbyist_expected_count,
            company_cache_count, lobbyist_cache_count
        ) VALUES (TRUE, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        [
            SCHEMA_VERSION,
            manifest.snapshot_id,
            manifest.fetch_started_at,
            manifest.source_fetched_at,
            built_at,
            manifest.company_start,
            manifest.company_stop,
            manifest.lobbyist_expected_count,
            company_cache_count,
            lobbyist_cache_count,
        ],
    )


def publish_snapshot(
    workspace: SnapshotWorkspace,
    *,
    live_db_path: Path,
) -> datetime:
    """Validate, timestamp, and atomically replace the served database."""
    candidate = workspace.candidate_db_path
    validate_database(candidate, require_published=False)
    published_at = datetime.now(UTC)
    conn = duckdb.connect(str(candidate))
    try:
        conn.execute("UPDATE snapshot_metadata SET published_at = ?", [published_at])
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    live_db_path.parent.mkdir(parents=True, exist_ok=True)
    previous_path = live_db_path.with_name(f"{live_db_path.stem}.previous{live_db_path.suffix}")
    if live_db_path.exists():
        _atomic_copy(live_db_path, previous_path)
    candidate.replace(live_db_path)
    validate_database(live_db_path, require_published=True)

    _archive_workspace(
        live_db_path.parent,
        workspace.root,
        workspace.manifest.snapshot_id,
    )
    _prune_archives(live_db_path.parent, keep=2)
    return published_at


def validate_database(path: Path, *, require_published: bool = True) -> SnapshotMetadata:
    """Open a database read-only and verify its schema and snapshot metadata."""
    if not path.is_file():
        raise SnapshotValidationError(f"DuckDB snapshot does not exist: {path}")
    conn = duckdb.connect(str(path), read_only=True)
    try:
        tables = {str(row[0]) for row in conn.execute("SHOW TABLES").fetchall()}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise SnapshotValidationError(
                "DuckDB snapshot is missing required tables: " + ", ".join(sorted(missing))
            )
        row = conn.execute(
            """
            SELECT schema_version, snapshot_id, fetch_started_at, source_fetched_at,
                   snapshot_built_at, published_at, company_start, company_stop,
                   lobbyist_expected_count, company_cache_count, lobbyist_cache_count
            FROM snapshot_metadata
            """
        ).fetchone()
        if row is None:
            raise SnapshotValidationError("DuckDB snapshot has no metadata row")
        if int(row[0]) != SCHEMA_VERSION:
            raise SnapshotValidationError(
                f"snapshot schema version {row[0]} is not supported version {SCHEMA_VERSION}"
            )
        if require_published and row[5] is None:
            raise SnapshotValidationError("DuckDB snapshot was never published")
        company_count = _scalar_count(conn.execute("SELECT COUNT(*) FROM companies").fetchone())
        lobbyist_count = _scalar_count(
            conn.execute("SELECT COUNT(*) FROM lobbyist_registrations").fetchone()
        )
        if company_count == 0 or lobbyist_count == 0:
            raise SnapshotValidationError("DuckDB snapshot contains an empty registry")
    finally:
        conn.close()
    return SnapshotMetadata(
        schema_version=int(row[0]),
        snapshot_id=str(row[1]),
        fetch_started_at=row[2],
        source_fetched_at=row[3],
        snapshot_built_at=row[4],
        published_at=row[5],
        company_start=int(row[6]),
        company_stop=int(row[7]),
        lobbyist_expected_count=row[8],
        company_cache_count=int(row[9]),
        lobbyist_cache_count=int(row[10]),
    )


def _validate_count_drop(
    live_path: Path,
    *,
    new_company_count: int,
    new_lobbyist_count: int,
    max_drop_fraction: float,
) -> None:
    if not live_path.exists():
        return
    try:
        conn = duckdb.connect(str(live_path), read_only=True)
        try:
            old_company_count = _scalar_count(
                conn.execute("SELECT COUNT(*) FROM companies").fetchone()
            )
            old_lobbyist_count = _scalar_count(
                conn.execute("SELECT COUNT(*) FROM lobbyist_registrations").fetchone()
            )
        finally:
            conn.close()
    except duckdb.Error as exc:
        raise SnapshotValidationError(f"cannot compare current snapshot counts: {exc}") from exc
    for label, old, new in (
        ("companies", old_company_count, new_company_count),
        ("lobbyists", old_lobbyist_count, new_lobbyist_count),
    ):
        minimum = int(old * (1 - max_drop_fraction))
        if old and new < minimum:
            raise SnapshotValidationError(
                f"{label} count dropped from {old} to {new}, beyond the allowed "
                f"{max_drop_fraction:.0%}; refusing publication"
            )


def _archive_workspace(data_dir: Path, root: Path, snapshot_id: str) -> Path:
    archive = data_dir / "snapshots" / snapshot_id
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if root.exists():
            raise SnapshotError(f"snapshot archive already exists: {archive}")
        return archive
    root.replace(archive)
    return archive


def _prune_archives(data_dir: Path, *, keep: int) -> None:
    """Retain only the newest immutable raw snapshots."""
    if keep < 1:
        raise ValueError("keep must be positive")
    snapshots_dir = data_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return
    archives = sorted(
        (path for path in snapshots_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for obsolete in archives[keep:]:
        shutil.rmtree(obsolete)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _scalar_count(row: tuple[object, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if not isinstance(value, int):
        raise SnapshotValidationError(f"expected integer count, got {type(value).__name__}")
    return value
