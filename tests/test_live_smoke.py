"""Live smoke tests against the real CADO site. Opt-in via ``CADO_LIVE_TESTS=1``.

These exist to catch upstream changes (e.g. the site adding a CAPTCHA, renaming
a field, or imposing a WAF) early — long before the bulk scraper hits them.
Keep them small and respectful: a handful of requests, no parallel fan-out.
"""

from __future__ import annotations

import pytest

from cado.http import CADOClient
from tests.conftest import live_required

pytestmark = live_required


async def test_can_fetch_company_search_form() -> None:
    async with CADOClient() as c:
        response = await c.get("/Company/CompanyNameNumberSearch.aspx")
        assert response.status_code == 200
        assert "txtNameKeywords1" in response.text
        assert c.last_viewstate is not None


async def test_keyword_search_returns_results() -> None:
    async with CADOClient() as c:
        await c.get("/Company/CompanyNameNumberSearch.aspx")
        response = await c.post_back(
            "/Company/CompanyNameNumberSearch.aspx",
            extra_fields={
                "txtNameKeywords1": "TIM",
                "txtNameKeywords2": "",
                "txtCompanyNumber": "",
            },
            button=("btnSearch", "10"),
        )
        assert "Records Found" in response.text
        assert "Name/Number Search Results" in response.text


@pytest.mark.parametrize("company_number", ["25166"])
async def test_number_search_jumps_directly_to_details(company_number: str) -> None:
    """A search by exact company number bypasses the result list and lands on
    CompanyDetails.aspx via a 302. This is the fast path the bulk enumerator
    uses (one POST per company instead of two)."""
    async with CADOClient() as c:
        await c.get("/Company/CompanyNameNumberSearch.aspx")
        response = await c.post_back(
            "/Company/CompanyNameNumberSearch.aspx",
            extra_fields={
                "txtNameKeywords1": "",
                "txtNameKeywords2": "",
                "txtCompanyNumber": company_number,
            },
            button=("btnSearch", "10"),
        )
        assert str(response.url).endswith("/CompanyDetails.aspx")
        assert f">{company_number}</span>" in response.text
