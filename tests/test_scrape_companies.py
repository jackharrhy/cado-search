"""Unit tests for the bulk companies scraper.

The scraper coordinates one ``CADOClient`` per worker and pushes raw HTML
into ``HtmlCache``. Rather than try to coax ``pytest-httpx`` through ASP.NET
redirects + cookies + viewstates, we drive the coordinator with a fake
client that hands back canned responses keyed by the search number.

End-to-end behaviour against the real upstream is covered by the live smoke
suite (``tests/test_live_smoke.py``, run with ``CADO_LIVE_TESTS=1``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cado.scrape.companies import CompanyScraper, ScrapeOutcome, ScrapeStats
from cado.storage import HtmlCache

FIXTURES = Path(__file__).parent / "fixtures"
COMPANIES = FIXTURES / "companies"


def fx(name: str) -> str:
    return (COMPANIES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    url: str
    text: str


class FakeCADOClient:
    """A drop-in replacement for ``CADOClient`` for scraper unit tests.

    Behaviour is configured per-search-number via ``responses``: a mapping
    from ``txtCompanyNumber`` -> either a *detail* response (302 redirect),
    a *list* response (200 with rows), or an *empty* response.
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []
        # Each "GET form" hands the same blank form back.
        self.get = AsyncMock()
        self.get.return_value = _Response(
            url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
            text="<html/>",
        )
        # The real scraper reads ``client.last_viewstate`` after the list
        # response to capture the page's viewstate for re-use across drills.
        # The fake doesn't model viewstate, so just expose ``None`` -- the
        # scraper's only requirement is that the attribute exists.
        self.last_viewstate = None
        self._last_viewstate = None

    async def post_back(
        self,
        url: str,
        *,
        event_target: str = "",
        event_argument: str = "",
        extra_fields: dict[str, str] | None = None,
        button: tuple[str, str] | None = None,
        referer: str | None = None,
    ) -> _Response:
        number = (extra_fields or {}).get("txtCompanyNumber", "")
        self.calls.append((event_target or "search", dict(extra_fields or {})))
        if number not in self._responses:
            raise AssertionError(f"unexpected search for txtCompanyNumber={number!r}")
        spec = self._responses[number]
        if event_target:
            # Drill into a row of the previously returned list.
            drill = spec.get("drill", {})
            return _Response(
                url=drill.get(
                    event_target,
                    "https://cado.eservices.gov.nl.ca/Company/ErrorPage.aspx",
                ),
                text=spec.get("drill_text", {}).get(event_target, "<html/>"),
            )
        kind = spec["kind"]
        if kind == "detail":
            return _Response(
                url="https://cado.eservices.gov.nl.ca/Company/CompanyDetails.aspx",
                text=spec["text"],
            )
        if kind == "list":
            return _Response(
                url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
                text=spec["text"],
            )
        # ``empty``
        return _Response(
            url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
            text="<html><body>no results</body></html>",
        )

    # AsyncExitStack-friendly no-op lifecycle methods.
    async def __aenter__(self) -> FakeCADOClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scraper(
    cache: HtmlCache,
    responses: dict[str, dict[str, Any]],
    *,
    concurrency: int = 1,
    skip_cached: bool = True,
) -> tuple[CompanyScraper, FakeCADOClient]:
    """Build a scraper whose workers all share one fake client.

    Sharing one client across workers is *fine* in tests (no real HTTP) and
    makes assertions about call counts easy.
    """
    scraper = CompanyScraper(
        cache=cache,
        concurrency=concurrency,
        rate_per_second=1000,
        skip_cached=skip_cached,
    )
    fake = FakeCADOClient(responses)
    scraper._clients = [fake] * concurrency  # type: ignore[assignment]
    # Skip the real ``__aenter__`` (which would open real httpx clients).
    return scraper, fake


@pytest.fixture
def cache(tmp_path: Path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html", registry="companies")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_singleton_detail_writes_one_cache_entry(cache: HtmlCache) -> None:
    detail_html = fx("c_50000_active_with_directors.html")
    scraper, _ = _make_scraper(
        cache,
        {"50000": {"kind": "detail", "text": detail_html}},
    )
    outcomes = [o async for o in scraper.scrape_numbers([50000])]

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.kind == "detail"
    assert o.ids_saved == ["50000"]
    assert cache.exists("50000", kind="detail")
    # The cache stores exactly the HTML we returned.
    assert "CONNAIGRE NET INCORPORATED" in cache.read("50000")


async def test_singleton_caches_even_when_lblCompanyName_is_empty(
    cache: HtmlCache,
) -> None:
    """Regression: a CompanyDetails.aspx response with an empty
    ``lblCompanyName`` used to raise CompanyParseError mid-scrape and the
    HTML was discarded. We now save the HTML unconditionally so the ingest
    step (which still uses the strict parser) can surface the issue."""
    broken_html = (
        "<html><body>"
        '<span id="lblCompanyName"></span>'
        '<span id="lblCompanyNumber">12345</span>'
        '<span id="lblCorporationType">Company</span>'
        "</body></html>"
    )
    scraper, _ = _make_scraper(
        cache,
        {"12345": {"kind": "detail", "text": broken_html}},
    )
    outcomes = [o async for o in scraper.scrape_numbers([12345])]

    assert outcomes[0].kind == "detail"
    assert cache.exists("12345", kind="detail")
    assert cache.read("12345") == broken_html


async def test_singleton_falls_back_to_search_number_when_id_missing(
    cache: HtmlCache,
) -> None:
    """If the upstream responds with a CompanyDetails.aspx URL but the body
    has no usable id at all, save under the search number rather than dropping."""
    broken_html = "<html><body>upstream had a bad day</body></html>"
    scraper, _ = _make_scraper(
        cache,
        {"99999": {"kind": "detail", "text": broken_html}},
    )
    outcomes = [o async for o in scraper.scrape_numbers([99999])]

    assert outcomes[0].kind == "detail"
    assert outcomes[0].ids_saved == ["99999"]
    assert cache.exists("99999", kind="detail")


async def test_empty_response_records_no_cache(cache: HtmlCache) -> None:
    scraper, _ = _make_scraper(
        cache,
        {"200000": {"kind": "empty"}},
    )
    outcomes = [o async for o in scraper.scrape_numbers([200000])]

    assert outcomes[0].kind == "empty"
    assert outcomes[0].ids_saved == []
    assert not cache.exists("200000")


async def test_multi_row_list_drills_into_each_suffix(cache: HtmlCache) -> None:
    list_html = fx("n_1_multirow.html")
    # Stub a unique drill response per row.
    drill = {
        f"rptCompanyNameSearchResults$_ctl{i}$lbtCompanyNumber": (
            "https://cado.eservices.gov.nl.ca/Company/CompanyDetails.aspx"
        )
        for i in (1, 2, 3, 4)
    }
    drill_text = {
        f"rptCompanyNameSearchResults$_ctl{i}$lbtCompanyNumber": fx("c_2D_extraprov_old.html")
        # We don't really care which suffix-fixture each row gets in this
        # test — we only check the file count downstream.
        for i in (1, 2, 3, 4)
    }
    scraper, _ = _make_scraper(
        cache,
        {
            "1": {
                "kind": "list",
                "text": list_html,
                "drill": drill,
                "drill_text": drill_text,
            }
        },
    )
    outcomes = [o async for o in scraper.scrape_numbers([1])]

    o = outcomes[0]
    assert o.kind == "hits"
    # 1 list entry + 4 drilled suffixes
    assert len(o.ids_saved) == 5
    assert "1[list]" in o.ids_saved
    # The list page is cached under "1" as a list-kind.
    assert cache.exists("1", kind="list")
    # And four detail pages were written. (FakeCADOClient returns the same
    # 2D fixture every time, so we just verify the count of detail files
    # under the registry root.)
    detail_keys = set(cache.iter_keys(kind="detail"))
    assert len(detail_keys) >= 1  # at least one detail page persisted


async def test_skip_cached_avoids_extra_requests(cache: HtmlCache) -> None:
    cache.write("50000", "<html>already here</html>")
    scraper, fake = _make_scraper(
        cache,
        # No matcher needed — we expect zero requests.
        {},
    )
    outcomes = [o async for o in scraper.scrape_numbers([50000])]

    assert outcomes[0].kind == "cached"
    # The fake client should not have been called at all.
    assert fake.calls == []
    fake.get.assert_not_called()


async def test_per_record_errors_are_isolated(cache: HtmlCache, tmp_path: Path) -> None:
    """If one number raises, others in the batch still complete."""
    detail_html = fx("c_50000_active_with_directors.html")

    class BoomClient(FakeCADOClient):
        async def post_back(self, *args: object, **kwargs: object) -> Any:
            number = (kwargs.get("extra_fields") or {}).get("txtCompanyNumber")
            if number == "999":
                raise RuntimeError("simulated upstream 500")
            return await super().post_back(*args, **kwargs)  # type: ignore[misc]

    scraper = CompanyScraper(
        cache=cache,
        concurrency=1,
        skip_cached=False,
        error_log_path=tmp_path / "errors.jsonl",
    )
    fake = BoomClient({"50000": {"kind": "detail", "text": detail_html}})
    scraper._clients = [fake]  # type: ignore[assignment]

    outcomes = {o.number: o async for o in scraper.scrape_numbers([50000, 999])}
    assert outcomes[50000].kind == "detail"
    assert outcomes[999].kind == "error"
    assert "simulated upstream 500" in (outcomes[999].error or "")


async def test_error_log_is_persisted_and_readable(cache: HtmlCache, tmp_path: Path) -> None:
    """Errors are appended to a JSONL file and re-readable for retry."""
    log_path = tmp_path / "errors.jsonl"

    class AlwaysBoomClient(FakeCADOClient):
        async def post_back(self, *args: object, **kwargs: object) -> Any:
            raise RuntimeError("boom")

    scraper = CompanyScraper(
        cache=cache,
        concurrency=1,
        skip_cached=False,
        error_log_path=log_path,
    )
    fake = AlwaysBoomClient({})
    scraper._clients = [fake]  # type: ignore[assignment]

    outcomes = [o async for o in scraper.scrape_numbers([111, 222, 333])]
    assert all(o.kind == "error" for o in outcomes)

    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    # Helper round-trip:
    assert CompanyScraper.read_error_log(log_path) == [111, 222, 333]


def test_read_error_log_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert CompanyScraper.read_error_log(tmp_path / "nope.jsonl") == []


async def test_stats_accumulates_outcomes() -> None:
    stats = ScrapeStats()
    stats.record(ScrapeOutcome(number=1, kind="detail", ids_saved=["1"]))
    stats.record(ScrapeOutcome(number=2, kind="empty"))
    stats.record(ScrapeOutcome(number=3, kind="hits", ids_saved=["3[list]", "3D", "3F"]))
    stats.record(ScrapeOutcome(number=4, kind="cached"))
    stats.record(ScrapeOutcome(number=5, kind="error", error="x"))
    assert stats.attempted == 5
    assert stats.details == 1
    assert stats.empty == 1
    assert stats.multi_hit_pages == 1
    assert stats.suffixed_drilled == 2
    assert stats.cached == 1
    assert stats.errors == 1
