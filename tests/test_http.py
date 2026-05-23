"""Tests for the HTTP client primitives.

These tests are purely offline (parsing + timing logic). The mocked HTTPX
plumbing is exercised in ``test_http_integration.py``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from cado.http import RateLimiter, ViewState, ViewStateError

FIXTURES = Path(__file__).parent / "fixtures"


class TestViewStateParse:
    def test_full_form_with_event_validation(self) -> None:
        html = (FIXTURES / "company_search_form.html").read_text()
        vs = ViewState.parse(html)
        assert vs.viewstate.startswith("/wEPDw")
        assert vs.generator == "FD17CE03"
        assert vs.event_validation is not None
        assert len(vs.event_validation) > 50

    def test_form_without_event_validation(self) -> None:
        # CompanyMain.aspx is a static page with viewstate but no event validation.
        html = """
            <input type="hidden" name="__VIEWSTATE" value="abc" />
            <input type="hidden" name="__VIEWSTATEGENERATOR" value="DEADBEEF" />
        """
        vs = ViewState.parse(html)
        assert vs.viewstate == "abc"
        assert vs.generator == "DEADBEEF"
        assert vs.event_validation is None

    def test_missing_viewstate_raises(self) -> None:
        with pytest.raises(ViewStateError, match="__VIEWSTATE"):
            ViewState.parse("<html><body>no form here</body></html>")

    def test_to_form_omits_none(self) -> None:
        vs = ViewState(viewstate="x", generator="y", event_validation=None)
        assert vs.to_form() == {"__VIEWSTATE": "x", "__VIEWSTATEGENERATOR": "y"}

    def test_to_form_includes_event_validation(self) -> None:
        vs = ViewState(viewstate="x", generator="y", event_validation="z")
        assert vs.to_form() == {
            "__VIEWSTATE": "x",
            "__VIEWSTATEGENERATOR": "y",
            "__EVENTVALIDATION": "z",
        }


class TestRateLimiter:
    async def test_first_acquire_is_immediate(self) -> None:
        limiter = RateLimiter(rate_per_second=10.0)
        start = time.perf_counter()
        await limiter.acquire()
        assert time.perf_counter() - start < 0.05

    async def test_enforces_minimum_interval(self) -> None:
        limiter = RateLimiter(rate_per_second=20.0)  # interval = 50 ms
        start = time.perf_counter()
        for _ in range(5):
            await limiter.acquire()
        elapsed = time.perf_counter() - start
        # 5 acquires at 20/s should take ~0.2s (4 intervals between 5 events).
        # Allow generous slack for slow CI.
        assert 0.15 < elapsed < 0.6, elapsed

    async def test_concurrent_acquires_are_serialised(self) -> None:
        limiter = RateLimiter(rate_per_second=10.0)  # 100 ms apart
        start = time.perf_counter()
        await asyncio.gather(*(limiter.acquire() for _ in range(4)))
        elapsed = time.perf_counter() - start
        # 4 events at 100 ms cadence => at least ~0.3 s (3 intervals).
        assert elapsed >= 0.25, elapsed

    def test_invalid_rate(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RateLimiter(0)
