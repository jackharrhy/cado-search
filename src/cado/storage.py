"""On-disk caching of raw scraped HTML.

The DuckDB schema is a *derived* artifact. Each immutable snapshot keeps its
raw pages as gzipped files so it can be validated or re-parsed without
re-hitting the upstream site. Files are sharded by the first two characters of
the id (or "00" for short ids) to avoid 100k+ files in one directory.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .settings import settings


def replace_bytes(path: Path, content: bytes) -> None:
    """Write bytes beside ``path``, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _shard(name: str) -> str:
    # Always use the first two ASCII chars, padding with "0".
    stem = name.upper()[:2]
    return stem if len(stem) == 2 else stem.ljust(2, "0")


class HtmlCache:
    """A gzipped-on-disk cache for scraped pages.

    Layout::

        data/html/
            companies/
                10/
                    10000.html.gz
                    10001.html.gz
                25/
                    25166.html.gz
                _list/
                    1.list.html.gz   # multi-row search result pages
            lobbyists/
                IH/
                    IHL-867-1005.html.gz
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        registry: str,
    ) -> None:
        self.root = (root or settings.html_cache_dir) / registry
        self.root.mkdir(parents=True, exist_ok=True)
        self._completed_outcomes: dict[str, str] | None = None

    # ---- paths --------------------------------------------------------

    def path_for(self, key: str, *, kind: str = "detail") -> Path:
        """Return the path where ``key`` would be cached.

        Parameters
        ----------
        key:
            The record's canonical id (e.g. ``"25166"``, ``"IHL-867-1005"``,
            ``"2D"``).
        kind:
            Either ``"detail"`` (default — a single record's detail page)
            or ``"list"`` (a multi-row search result we kept for reference).
            ``"list"`` entries go under a dedicated ``_list`` subdirectory
            so the detail/list cardinality stays unambiguous.
        """
        safe = _safe_name(key)
        if kind == "list":
            return self.root / "_list" / f"{safe}.list.html.gz"
        return self.root / _shard(safe) / f"{safe}.html.gz"

    # ---- IO -----------------------------------------------------------

    def exists(self, key: str, *, kind: str = "detail") -> bool:
        return self.path_for(key, kind=kind).exists()

    def write(self, key: str, html: str, *, kind: str = "detail") -> Path:
        path = self.path_for(key, kind=kind)
        # Gzip with mtime=0 and a fixed compression level so the same input
        # produces a byte-identical file — keeps diffs / hashes stable.
        compressed = gzip.compress(html.encode(), compresslevel=6, mtime=0)
        replace_bytes(path, compressed)
        return path

    def read(self, key: str, *, kind: str = "detail") -> str:
        path = self.path_for(key, kind=kind)
        with gzip.open(path, "rb") as fh:
            return fh.read().decode("utf-8")

    def delete(self, key: str, *, kind: str = "detail") -> bool:
        path = self.path_for(key, kind=kind)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ---- resumable search-seed completion -----------------------------

    @property
    def completion_log_path(self) -> Path:
        """Append-only journal of fully processed upstream search seeds."""
        return self.root / "_completed.jsonl"

    def is_completed(self, key: str) -> bool:
        """Return whether every result for search seed ``key`` was persisted.

        An exact detail file can safely be treated as complete, including in a
        legacy cache without a journal. A list page is deliberately insufficient:
        an interrupted multi-row drill may have saved only some of its rows.
        """
        if self._completed_outcomes is None:
            self._completed_outcomes = self._read_completed_outcomes()
        return key in self._completed_outcomes or self.exists(key, kind="detail")

    def completed_outcome(self, key: str) -> str | None:
        """Return the journaled outcome for ``key``, if it is complete."""
        if self._completed_outcomes is None:
            self._completed_outcomes = self._read_completed_outcomes()
        return self._completed_outcomes.get(key)

    def mark_completed(
        self,
        key: str,
        *,
        outcome: str,
        detail_keys: list[str] | None = None,
    ) -> None:
        """Journal a successful detail/list/empty search result.

        One compact append is used so a torn final line can simply be ignored
        on resume. Callers must invoke this only after every referenced cache
        file has been atomically persisted.
        """
        if self._completed_outcomes is None:
            self._completed_outcomes = self._read_completed_outcomes()
        payload: dict[str, Any] = {
            "key": key,
            "outcome": outcome,
            "detail_keys": detail_keys or [],
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        # Binary append is one small write. If the process dies mid-write, the
        # reader ignores that malformed line and retries the seed.
        with self.completion_log_path.open("ab", buffering=0) as fh:
            fh.write(encoded)
        self._completed_outcomes[key] = outcome

    def _read_completed_outcomes(self) -> dict[str, str]:
        path = self.completion_log_path
        if not path.exists():
            return {}
        completed: dict[str, str] = {}
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    payload = json.loads(line)
                    key = payload["key"]
                    outcome = payload["outcome"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    # A process may have died during the final append. Earlier
                    # complete lines remain valid and the torn seed is retried.
                    continue
                if isinstance(key, str) and isinstance(outcome, str):
                    completed[key] = outcome
        return completed

    # ---- enumeration --------------------------------------------------

    def iter_keys(self, *, kind: str = "detail") -> Iterator[str]:
        """Yield every cached key for the given kind, in arbitrary order."""
        if kind == "list":
            base = self.root / "_list"
            suffix = ".list.html.gz"
        else:
            base = self.root
            suffix = ".html.gz"
        if not base.exists():
            return
        for path in base.rglob(f"*{suffix}"):
            if kind == "detail" and path.parent.name == "_list":
                continue
            yield path.name[: -len(suffix)]


def _safe_name(key: str) -> str:
    """Make ``key`` safe for the filesystem.

    The cado id space uses digits + uppercase letters + hyphens (lobbyist ids
    like ``IHL-867-1005``); the only thing we need to defend against is the
    odd record with whitespace or a slash. Replace anything outside
    ``[A-Za-z0-9._-]`` with ``_``.
    """
    out = []
    for ch in key:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)
