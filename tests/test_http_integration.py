"""Integration tests for ``CADOClient`` using mocked HTTPX responses."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from cado.http import CADOClient

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fast_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Don't let rate limiting slow these tests down.
    monkeypatch.setenv("CADO_REQUESTS_PER_SECOND", "1000")
    monkeypatch.setenv("CADO_MAX_CONCURRENCY", "8")
    monkeypatch.setenv("CADO_RETRIES", "1")


async def test_get_captures_viewstate(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        text=(FIXTURES / "company_search_form.html").read_text(),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    async with CADOClient() as c:
        await c.get("/Company/CompanyNameNumberSearch.aspx")
        vs = c.last_viewstate
        assert vs is not None
        assert vs.generator == "FD17CE03"


async def test_post_back_sends_required_fields(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        text=(FIXTURES / "company_search_form.html").read_text(),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        text=(FIXTURES / "company_search_results_tim.html").read_text(),
        headers={"content-type": "text/html; charset=utf-8"},
    )

    async with CADOClient() as c:
        await c.get("/Company/CompanyNameNumberSearch.aspx")
        await c.post_back(
            "/Company/CompanyNameNumberSearch.aspx",
            extra_fields={"txtNameKeywords1": "TIM"},
            button=("btnSearch", "10"),
        )

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1
    body = posts[0].content.decode()
    # All four required fields are present
    assert "__VIEWSTATE=" in body
    assert "__VIEWSTATEGENERATOR=FD17CE03" in body
    assert "__EVENTVALIDATION=" in body
    assert "txtNameKeywords1=TIM" in body
    # Image button coords
    assert "btnSearch.x=10" in body
    assert "btnSearch.y=10" in body
    # Referer is set
    assert posts[0].headers["Referer"].endswith("/Company/CompanyNameNumberSearch.aspx")


async def test_post_back_without_prior_get_raises() -> None:
    async with CADOClient() as c:
        with pytest.raises(RuntimeError, match="before any GET"):
            await c.post_back("/Company/CompanyNameNumberSearch.aspx")


async def test_post_back_chains_updated_viewstate(httpx_mock: HTTPXMock) -> None:
    """After a postback the response's *new* viewstate must replace the old one."""
    httpx_mock.add_response(
        method="GET",
        url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        text=(FIXTURES / "company_search_form.html").read_text(),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://cado.eservices.gov.nl.ca/Company/CompanyNameNumberSearch.aspx",
        text=(FIXTURES / "company_search_results_tim.html").read_text(),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    async with CADOClient() as c:
        await c.get("/Company/CompanyNameNumberSearch.aspx")
        first_vs = c.last_viewstate
        await c.post_back(
            "/Company/CompanyNameNumberSearch.aspx",
            extra_fields={"txtNameKeywords1": "TIM"},
            button=("btnSearch", "10"),
        )
        second_vs = c.last_viewstate

    assert first_vs is not None and second_vs is not None
    # The fixtures were captured at different times — viewstates must differ.
    assert first_vs.viewstate != second_vs.viewstate
