"""On-disk caching of raw scraped HTML.

The DuckDB schema is a *derived* artifact — we keep every raw page we ever
fetched as a gzipped file under ``data/html/`` so we can re-parse with an
updated parser without re-hitting the upstream site. Files are sharded by the
first two characters of the id (or "00" for short ids) to avoid 100k+ files
in a single directory.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

from .settings import settings


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
        path.parent.mkdir(parents=True, exist_ok=True)
        # Gzip with mtime=0 and a fixed compression level so the same input
        # produces a byte-identical file — keeps diffs / hashes stable.
        with (
            path.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as fh,
        ):
            fh.write(html.encode("utf-8"))
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
