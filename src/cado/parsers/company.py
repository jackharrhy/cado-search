"""Parse CADO company / condo / co-op detail and search-result pages.

The upstream HTML is brittle and inconsistent: most data lives in
``<span id="lblXxx">value</span>`` elements that we look up by id, but
addresses interleave bare text with ``<br>`` and several fields are simply
absent on some records. The strategy is therefore:

* prefer ``soup.find(id=...)`` over CSS / XPath — ids are stable
* normalise whitespace aggressively (strip + collapse) on every text read
* treat any blank string as ``None`` so downstream code doesn't have to care
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from bs4 import BeautifulSoup, Tag

from ..models import (
    Address,
    Category,
    Company,
    CompanySearchHit,
    CompanySearchResult,
    CorporationType,
    Director,
    PreviousName,
)


class CompanyParseError(RuntimeError):
    """Raised when a page we expected to be a CompanyDetails.aspx isn't."""


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """The high-level shape of a response to a name/number search.

    The upstream behaves three ways depending on how many records match
    a number search:

    * exactly one record  -> 302 to ``/Company/CompanyDetails.aspx`` (``details``)
    * 2+ records          -> 200, the form is re-rendered with a result list (``hits``)
    * zero records        -> 200, the form is re-rendered, no result list (``empty``)
    """

    kind: str  # "details" | "hits" | "empty"
    details: Company | None = None
    hits: CompanySearchResult | None = None


def parse_search_response(html: str, *, final_url: str | None = None) -> SearchResponse:
    """Classify and parse whichever shape of search response we received.

    Parameters
    ----------
    html:
        The body of the final HTTP response after following redirects.
    final_url:
        The URL of that final response. Used to disambiguate: when the upstream
        sends us to ``CompanyDetails.aspx`` the parser uses the detail path;
        otherwise we look for a result table in the form page.
    """
    if final_url and "CompanyDetails.aspx" in final_url:
        return SearchResponse(kind="details", details=parse_company_details(html))

    soup = BeautifulSoup(html, "lxml")
    table = soup.find(id="tableSearchResults")
    if table is None:
        return SearchResponse(kind="empty")

    hits = parse_company_search_results(html, _soup=soup)
    if not hits.hits:
        return SearchResponse(kind="empty")
    return SearchResponse(kind="hits", hits=hits)


def extract_company_number(html: str) -> str | None:
    """Pull just the canonical id from a CompanyDetails.aspx page.

    Useful for the scraper, which only needs to know what filename to save
    under -- it shouldn't refuse to cache HTML just because some other field
    (e.g. ``lblCompanyName``) is empty on a malformed upstream response.
    Returns ``None`` if the page isn't a details page at all.
    """
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(id="lblCompanyNumber")
    if el is None:
        return None
    text = _collapse(el.get_text())
    return text or None


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------


_DATE_FORMATS: Final = ("%Y-%m-%d",)

# Map ``lblXxx`` ids straight to ``Company`` field names where the relationship
# is a 1:1 trim-and-keep.
_SIMPLE_FIELDS: Final[dict[str, str]] = {
    "lblStatus": "status",
    "lblBusinessType": "business_type",
    "lblIncorporationJurisdiction": "incorporation_jurisdiction",
    "lblFilingType": "filing_type",
    "lblMinMaxDirectors": "min_max_directors",
    "lblAddInfo": "additional_info",
}


def parse_company_details(html: str) -> Company:
    """Parse a single ``CompanyDetails.aspx`` page into a :class:`Company`."""
    soup = BeautifulSoup(html, "lxml")

    if soup.find(id="lblCompanyName") is None or soup.find(id="lblCompanyNumber") is None:
        raise CompanyParseError("page is not a CompanyDetails.aspx (no lblCompanyName)")

    number = _text(soup, "lblCompanyNumber")
    if not number:
        raise CompanyParseError("lblCompanyNumber is empty")

    name = _text(soup, "lblCompanyName")
    if not name:
        raise CompanyParseError("lblCompanyName is empty")

    corp_type_text = _text(soup, "lblCorporationType")
    try:
        corp_type = CorporationType(corp_type_text)
    except ValueError as exc:
        raise CompanyParseError(f"unknown CorporationType: {corp_type_text!r}") from exc

    category: Category | None = None
    cat_text = _text(soup, "lblCategory")
    if cat_text:
        try:
            category = Category(cat_text)
        except ValueError:
            category = None  # tolerate unexpected values rather than blowing up

    company = Company(
        number=number,
        name=name,
        corporation_type=corp_type,
        category=category,
        incorporation_date=_date(soup, "lblIncorporationDate"),
        registration_date=_date(soup, "lblRegistrationDate"),
        last_annual_return=_date(soup, "lblLastAnnualReturn"),
        registered_office=_parse_address(soup, prefix="RO"),
        mailing_address=_parse_address(soup, prefix="MA"),
        mailing_same_as_registered=bool(_text(soup, "lblMASameAsRegistered")),
        directors=_parse_directors(soup),
        previous_names=_parse_previous_names(soup),
        historical_remarks=_parse_historical_remarks(soup),
        **_collect_simple_fields(soup),
    )
    return company


# ---------------------------------------------------------------------------
# Search results parser
# ---------------------------------------------------------------------------


_ROW_NUM_RX = re.compile(r"rptCompanyNameSearchResults__ctl(\d+)_lbtCompanyNumber")
_COMPANY_NUMBER_RX = re.compile(r"^([0-9]+[A-Z]*)$")


def parse_company_search_results(
    html: str, *, _soup: BeautifulSoup | None = None
) -> CompanySearchResult:
    """Parse the result-list variant of CompanyNameNumberSearch.aspx.

    The page contains one row per matching record. Each row carries:

    * a ``<a>`` linking to the detail page (text = ``lblCompanyName``)
    * a ``<span>`` with the status (``lblStatusItem``)
    * a ``<td class="CompanyNumberText">`` with the company number, possibly
      suffixed (``"2D"``, ``"100CM"``)
    * the corporation type as plain ``<td>`` text
    * a ``<span>`` with the display date (``lblDisplayDateItem``)
    """
    soup = _soup if _soup is not None else BeautifulSoup(html, "lxml")

    table = soup.find(id="tableSearchResults")
    if table is None:
        return CompanySearchResult(total=0, viewing="0-0", hits=[])
    assert isinstance(table, Tag)

    # ``lblRecordsFound`` / ``lblViewingRecords`` appear once at the top of
    # the result table — but identical ids reappear on detail pages for the
    # nested directors/previous-names sub-tables. Scoping to the search table
    # avoids collisions.
    total_el = table.find(id="lblRecordsFound")
    viewing_el = table.find(id="lblViewingRecords")
    total = int(_collapse(total_el.get_text() if total_el else "0") or "0")
    viewing = _collapse(viewing_el.get_text() if viewing_el else "") or "0-0"

    hits: list[CompanySearchHit] = []
    for anchor in table.find_all("a", id=_ROW_NUM_RX):
        assert isinstance(anchor, Tag)
        m = _ROW_NUM_RX.search(anchor.get("id", ""))
        if not m:
            continue
        row_index = int(m.group(1))

        tr = anchor.find_parent("tr")
        if not isinstance(tr, Tag):
            continue
        tds = tr.find_all("td", recursive=False)
        # Expected columns: [name | status | number | corp_type | date]
        if len(tds) < 5:
            continue

        name = _collapse(anchor.get_text())
        status = _collapse(tds[1].get_text())
        number_text = _collapse(tds[2].get_text())
        corp_type = _collapse(tds[3].get_text())
        date_text = _collapse(tds[4].get_text())

        if not _COMPANY_NUMBER_RX.match(number_text):
            # Skip rows that don't look like a real id (defensive).
            continue

        hits.append(
            CompanySearchHit(
                name=name,
                number=number_text,
                row_index=row_index,
                status=status,
                corporation_type=corp_type,
                date_text=date_text or None,
            )
        )

    return CompanySearchResult(total=total, viewing=viewing, hits=hits)


# ---------------------------------------------------------------------------
# Sub-section parsers
# ---------------------------------------------------------------------------


def _parse_address(soup: BeautifulSoup, *, prefix: str) -> Address:
    return Address(
        contact=_text(soup, f"lbl{prefix}Contact"),
        line1=_text(soup, f"lbl{prefix}Address1"),
        line2=_text(soup, f"lbl{prefix}Address2"),
        line3=_text(soup, f"lbl{prefix}Address3"),
        city=_text(soup, f"lbl{prefix}City"),
        province_state=_text(soup, f"lbl{prefix}ProvinceState"),
        country=_text(soup, f"lbl{prefix}Country"),
        postal_zip=_text(soup, f"lbl{prefix}PostalZipCode"),
    )


def _parse_directors(soup: BeautifulSoup) -> list[Director]:
    panel = soup.find(id="pnlCurrentDirectors")
    if panel is None:
        return []
    assert isinstance(panel, Tag)
    # The director rows live in the *innermost* table within the panel; the
    # outer tables are layout chrome that also contain "row"-classed wrappers
    # with the section header, records-found chrome, etc.
    inner = panel.find("table", id=None)
    if inner is None:
        # Fall back to the outermost table; same row scan, just slightly more
        # work for the filter logic to do.
        inner = panel
    assert isinstance(inner, Tag)

    directors: list[Director] = []
    for tr in inner.find_all("tr"):
        classes = tr.get("class") or []
        if not any(c in {"row", "rowalt"} for c in classes):
            continue
        # Director rows in the inner table have exactly one <td> whose text is
        # a name spread across two lines ("Mark\n\t\t\t\tCourtney").
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 1:
            continue
        full = _collapse(tds[0].get_text(separator=" "))
        if not full:
            continue
        lower = full.lower()
        if "records found" in lower or "viewing records" in lower or lower == "director name":
            continue
        parts = full.split(maxsplit=1)
        first = parts[0] if len(parts) >= 1 else None
        last = parts[1] if len(parts) == 2 else None
        directors.append(Director(full_name=full, first_name=first, last_name=last))
    return directors


def _parse_previous_names(soup: BeautifulSoup) -> list[PreviousName]:
    panel = soup.find(id="pnlPreviousCompanyNames")
    if panel is None:
        return []
    assert isinstance(panel, Tag)
    out: list[PreviousName] = []
    # Each row has columns: name, effective date.
    for tr in panel.find_all("tr"):
        classes = tr.get("class", []) or []
        if not any(c in {"row", "rowalt"} for c in classes):
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        name = _collapse(tds[0].get_text())
        if not name or name.lower().startswith("previous"):  # skip header
            continue
        effective: date | None = None
        if len(tds) > 1:
            effective = _to_date(_collapse(tds[1].get_text()))
        out.append(PreviousName(name=name, effective_date=effective))
    return out


def _parse_historical_remarks(soup: BeautifulSoup) -> list[str]:
    table = soup.find(id="tblHistoricalRemarks")
    if table is None:
        return []
    assert isinstance(table, Tag)
    remarks: list[str] = []
    for tr in table.find_all("tr"):
        classes = tr.get("class", []) or []
        if not any(c in {"row", "rowalt"} for c in classes):
            continue
        text = _collapse(tr.get_text(separator=" "))
        # Skip the section header row that just says "Historical Remarks".
        if text and text.lower() != "historical remarks":
            remarks.append(text)
    return remarks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WS_RX = re.compile(r"\s+")


def _collapse(text: str | None) -> str:
    if text is None:
        return ""
    return _WS_RX.sub(" ", text).strip()


def _text(soup: BeautifulSoup, element_id: str) -> str | None:
    el = soup.find(id=element_id)
    if el is None:
        return None
    return _collapse(el.get_text()) or None


def _date(soup: BeautifulSoup, element_id: str) -> date | None:
    return _to_date(_text(soup, element_id))


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _collect_simple_fields(soup: BeautifulSoup) -> dict[str, str | None]:
    return {field: _text(soup, label) for label, field in _SIMPLE_FIELDS.items()}
