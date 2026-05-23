"""Live tests for the lobbyist scraper. Opt-in via ``CADO_LIVE_TESTS=1``."""

from __future__ import annotations

from pathlib import Path

import pytest

from cado.parsers import parse_lobbyist_detail
from cado.scrape.lobbyists import LobbyistScraper
from cado.storage import HtmlCache
from tests.conftest import live_required

pytestmark = live_required


@pytest.fixture
def cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="lobbyists")


async def test_build_index_returns_real_entries(cache: HtmlCache) -> None:
    async with LobbyistScraper(cache=cache, rate_per_second=5) as scr:
        total, entries = await scr.build_index()

    assert total is not None
    assert total > 0
    assert len(entries) == total
    # Pagination is at 10 per page; we should have at least one entry past
    # the first page.
    assert max(e.page_no for e in entries) > 1
    # Registration numbers are well-formed.
    for e in entries[:5]:
        assert "-" in e.registration_number


async def test_scrape_first_few_details(cache: HtmlCache) -> None:
    async with LobbyistScraper(cache=cache, rate_per_second=5) as scr:
        _total, entries = await scr.build_index()
        # Take 3 to keep traffic minimal.
        outcomes = [o async for o in scr.scrape_details(entries[:3])]

    assert len(outcomes) == 3
    for outcome in outcomes:
        assert outcome.kind in ("detail", "skipped")
        if outcome.kind == "detail":
            html = cache.read(outcome.registration_number)
            reg = parse_lobbyist_detail(html)
            assert reg.registration_number == outcome.registration_number
