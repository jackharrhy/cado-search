"""Live end-to-end scrape tests. Opt-in via ``CADO_LIVE_TESTS=1``.

These exercise the bulk scraper against the real upstream with a small,
deterministic set of numbers — five real records that we expect to remain
stable for years. A few seconds of real traffic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cado.parsers import parse_company_details
from cado.scrape.companies import CompanyScraper
from cado.storage import HtmlCache
from tests.conftest import live_required

pytestmark = live_required


@pytest.fixture
def cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="companies")


async def test_small_live_scrape(cache: HtmlCache) -> None:
    """Scrape 5 known-good companies and assert each round-trips through the parser."""
    targets = [25166, 50000, 73498, 99000, 99900]
    async with CompanyScraper(cache=cache, concurrency=2, rate_per_second=5) as scr:
        outcomes = {o.number: o async for o in scr.scrape_numbers(targets)}

    assert set(outcomes) == set(targets)
    for n, outcome in outcomes.items():
        # Every one of these is a known single-record id.
        assert outcome.kind == "detail", f"{n}: {outcome}"
        assert outcome.ids_saved == [str(n)]
        # And re-parse the cached HTML.
        company = parse_company_details(cache.read(str(n)))
        assert company.number == str(n)
        assert company.name


async def test_live_scrape_handles_multi_row(cache: HtmlCache) -> None:
    """Number 2 returns a multi-row list whose suffixed rows we drill into."""
    async with CompanyScraper(cache=cache, concurrency=1, rate_per_second=5) as scr:
        outcomes = [o async for o in scr.scrape_numbers([2])]

    assert outcomes[0].kind == "hits"
    # We should have saved at least 2 detail pages (one per distinct suffix).
    detail_keys = set(cache.iter_keys(kind="detail"))
    assert len(detail_keys) >= 2
    # And the list page itself.
    assert cache.exists("2", kind="list")
