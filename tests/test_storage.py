"""Tests for the HTML cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from cado.storage import HtmlCache


@pytest.fixture
def cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="companies")


class TestHtmlCache:
    def test_roundtrip_detail(self, cache: HtmlCache) -> None:
        cache.write("25166", "<html>hello</html>")
        assert cache.exists("25166")
        assert cache.read("25166") == "<html>hello</html>"

    def test_roundtrip_list(self, cache: HtmlCache) -> None:
        cache.write("1", "<html>multi</html>", kind="list")
        assert cache.exists("1", kind="list")
        # Detail and list don't collide.
        assert not cache.exists("1", kind="detail")
        assert cache.read("1", kind="list") == "<html>multi</html>"

    def test_sharding_by_first_two_chars(self, cache: HtmlCache) -> None:
        cache.write("99000", "<html/>")
        cache.write("2D", "<html/>")
        cache.write("IHL-867-1005", "<html/>")
        assert cache.path_for("99000").parent.name == "99"
        assert cache.path_for("2D").parent.name == "2D"
        assert cache.path_for("IHL-867-1005").parent.name == "IH"

    def test_iter_keys_only_returns_details(self, cache: HtmlCache) -> None:
        cache.write("25166", "<html/>")
        cache.write("99000", "<html/>")
        cache.write("1", "<html/>", kind="list")
        keys = set(cache.iter_keys())
        assert keys == {"25166", "99000"}
        assert set(cache.iter_keys(kind="list")) == {"1"}

    def test_safe_name_handles_unusual_characters(self, cache: HtmlCache) -> None:
        cache.write("weird/key with space", "<html>x</html>")
        # We can still round-trip it under the sanitised name.
        assert cache.read("weird/key with space") == "<html>x</html>"

    def test_delete(self, cache: HtmlCache) -> None:
        cache.write("25166", "<html/>")
        assert cache.delete("25166") is True
        assert not cache.exists("25166")
        assert cache.delete("25166") is False

    def test_files_are_actually_gzipped(self, cache: HtmlCache) -> None:
        cache.write("25166", "<html>" + "x" * 10_000 + "</html>")
        on_disk = cache.path_for("25166").stat().st_size
        # 10 KB of repeated 'x' should compress to a few hundred bytes.
        assert on_disk < 1_000

    def test_deterministic_gzip_output(self, cache: HtmlCache) -> None:
        # mtime=0 means the same input always hashes the same.
        cache.write("25166", "<html>hi</html>")
        bytes_a = cache.path_for("25166").read_bytes()
        cache.write("25166", "<html>hi</html>")
        bytes_b = cache.path_for("25166").read_bytes()
        assert bytes_a == bytes_b
