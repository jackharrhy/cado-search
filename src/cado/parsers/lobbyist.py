"""Parsers for the Registry of Lobbyists pages.

The lobbyist registry uses the same ``lblXxx`` convention as the company
registry but with a different field set. There are two relevant pages:

* ``LobbyistSearch.aspx`` — both the form and the paginated result table.
* ``lobbySummary.aspx``  — the detail page reached by drilling into a row.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Final

from bs4 import BeautifulSoup, Tag

from ..models import (
    Address,
    InHouseLobbyist,
    LobbyingActivity,
    LobbyistRegistration,
    LobbyistSearchHit,
)


class LobbyistParseError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


def parse_lobbyist_detail(html: str) -> LobbyistRegistration:
    soup = BeautifulSoup(html, "lxml")
    raw = _collect_all_labels(soup)

    reg_num = raw.get("lblRegistrationNumber")
    if not reg_num:
        raise LobbyistParseError("page is not a lobbySummary.aspx (no lblRegistrationNumber)")

    return LobbyistRegistration(
        registration_number=reg_num,
        status=raw.get("lblstatus") or raw.get("lblStatus"),
        lobbyist_type=_infer_lobbyist_type(reg_num, raw),
        registration_date=_to_date(raw.get("lblRegistrationDate")),
        effective_date=_to_date(raw.get("lblEffectiveDate")),
        amended_date=_to_date(raw.get("lblAmendedDate")),
        approval_date=_to_date(raw.get("lblApprovalDate")),
        contact_name=raw.get("lblContactName"),
        contact_address=Address(
            line1=raw.get("lblContactAddressCI"),
            city=raw.get("lblContactCityCI"),
            province_state=raw.get("lblContactProvince"),
            postal_zip=raw.get("lblContactPostal"),
        ),
        firm_name=raw.get("lblFirmName"),
        firm_address=Address(
            line1=raw.get("lblAddressF"),
            city=raw.get("lblCityF"),
            province_state=raw.get("lblProvinceStateF"),
            postal_zip=raw.get("lblPostalZipCodeF"),
        ),
        particulars=raw.get("lblParticulars"),
        organization_description=raw.get("lblOrgDesc"),
        organization_membership=raw.get("lblOrgMembership"),
        subject_matters=_activity_rows(
            raw,
            prefix="rptSubjectMatter",
            name_label="lblSubjectMatter",
            has_label="lblHas",
            expects_label="lblExpects",
        ),
        lobbying_targets=_activity_rows(
            raw,
            prefix="rptTargets",
            name_label="lblTargets",
            has_label="lblTarHas",
            expects_label="lblTarExpects",
        ),
        communication_techniques=_activity_rows(
            raw,
            prefix="rptCommTech",
            name_label="lblCommTech",
            has_label="lblCommHas",
            expects_label="lblCommExpects",
        ),
        in_house_lobbyists=_in_house_rows(raw),
        raw_fields=raw,
    )


_WS_RX: Final = re.compile(r"[ \t]+")


def _collect_all_labels(soup: BeautifulSoup) -> dict[str, str]:
    """Snapshot every simple or repeater label on the page.

    We use ``separator='\\n'`` and *only* collapse intra-line whitespace so
    multi-line text (the free-form ``lblOrgDesc`` field, for instance) keeps
    its line breaks.
    """
    out: dict[str, str] = {}
    for span in soup.find_all(id=re.compile(r"^(?:lbl|rpt.+_lbl)", re.IGNORECASE)):
        assert isinstance(span, Tag)
        sid = span.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        text = span.get_text(separator="\n")
        # Collapse runs of spaces/tabs per line, strip surrounding whitespace.
        text = "\n".join(_WS_RX.sub(" ", line).strip() for line in text.splitlines())
        text = text.strip()
        if text:
            out[sid] = text
    return out


def _activity_rows(
    raw: dict[str, str],
    *,
    prefix: str,
    name_label: str,
    has_label: str,
    expects_label: str,
) -> list[LobbyingActivity]:
    rows: list[LobbyingActivity] = []
    pattern = re.compile(rf"^{re.escape(prefix)}__ctl(\d+)_{re.escape(name_label)}$")
    matches: list[tuple[int, str]] = []
    for key, value in raw.items():
        match = pattern.match(key)
        if match:
            matches.append((int(match.group(1)), value))
    for index, name in sorted(matches):
        row_prefix = f"{prefix}__ctl{index}_"
        rows.append(
            LobbyingActivity(
                name=name,
                has_lobbied=_yes_no(raw.get(row_prefix + has_label)),
                expects_to_lobby=_yes_no(raw.get(row_prefix + expects_label)),
            )
        )
    return rows


def _in_house_rows(raw: dict[str, str]) -> list[InHouseLobbyist]:
    pattern = re.compile(r"^rptInHouseLobbyists__ctl(\d+)_lblInHouseLobbyist$")
    matches: list[tuple[int, str]] = []
    for key, value in raw.items():
        match = pattern.match(key)
        if match:
            matches.append((int(match.group(1)), value))
    return [
        InHouseLobbyist(
            name=name,
            effective_date=_to_date(
                raw.get(f"rptInHouseLobbyists__ctl{index}_lblinHouseEffective")
            ),
            inactive_date=_to_date(raw.get(f"rptInHouseLobbyists__ctl{index}_lblinHouseInactive")),
        )
        for index, name in sorted(matches)
    ]


def _yes_no(value: str | None) -> bool | None:
    if value is None:
        return None
    normalised = value.strip().casefold()
    if normalised == "yes":
        return True
    if normalised == "no":
        return False
    return None


def _infer_lobbyist_type(reg_num: str, raw: dict[str, str]) -> str | None:
    if "lblLobbyistType" in raw:
        return raw["lblLobbyistType"]
    # Registration numbers themselves encode the type: 'IHL-...' = In-House
    # Lobbyist, 'CL-...' = Consultant Lobbyist.
    prefix = reg_num.split("-", 1)[0].upper()
    if prefix == "CL":
        return "Consultant"
    if prefix == "IHL":
        return "In-House"
    return None


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Search-results page
# ---------------------------------------------------------------------------


_ROW_REG_RX = re.compile(r"rptSearchResults__ctl(\d+)_lbtRegNum")


def parse_lobbyist_search_results(html: str) -> list[LobbyistSearchHit]:
    """Parse a single page of the lobbyist Search All results.

    The page is paginated at 10 rows; the caller drives pagination by issuing
    a ``__doPostBack('lbtNext')`` between calls.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[LobbyistSearchHit] = []
    for anchor in soup.find_all("a", id=_ROW_REG_RX):
        assert isinstance(anchor, Tag)
        anchor_id = anchor.get("id")
        if not isinstance(anchor_id, str):
            continue
        m = _ROW_REG_RX.search(anchor_id)
        if not m:
            continue
        row = int(m.group(1))
        tr = anchor.find_parent("tr")
        if not isinstance(tr, Tag):
            continue
        tds = tr.find_all("td", recursive=False)
        # Columns: [reg# | name | type | client | company/org | date | status]
        if len(tds) < 7:
            continue
        out.append(
            LobbyistSearchHit(
                registration_number=_collapse(anchor.get_text()),
                row_index=row,
                name=_collapse(tds[1].get_text()),
                lobbyist_type=_collapse(tds[2].get_text()),
                client=_collapse(tds[3].get_text()) or None,
                organization=_collapse(tds[4].get_text()) or None,
                activity_date_text=_collapse(tds[5].get_text()) or None,
                status=_collapse(tds[6].get_text()) or None,
            )
        )
    return out


def get_total_records(html: str) -> int | None:
    """Extract ``lblRecordsFound`` from a search-results page."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(id="lblRecordsFound")
    if el is None:
        return None
    try:
        return int(_collapse(el.get_text()))
    except ValueError:
        return None


def get_viewing_range(html: str) -> tuple[int, int] | None:
    """Return ``(first, last)`` from the ``lblViewingRecords`` span.

    The upstream renders ``1-10``, ``11-20``, ..., ``721-727`` and *also* keeps
    rendering ``Next >`` even when you're on the last page. The pager treats
    "Next from the last page" as a no-op — so we detect end-of-list by
    watching ``lblViewingRecords`` instead of the link.
    """
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(id="lblViewingRecords")
    if el is None:
        return None
    text = _collapse(el.get_text())
    m = re.match(r"^(\d+)-(\d+)$", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def has_next_page(html: str) -> bool:
    """Whether the current results page has more pages after it.

    Implementation note: ``lbtNext`` is always rendered on every page, so we
    must compare ``viewing`` against ``total``. ``has_next_page("1-10", total=727)``
    is True; ``has_next_page("721-727", total=727)`` is False.
    """
    viewing = get_viewing_range(html)
    total = get_total_records(html)
    if viewing is None or total is None:
        return False
    return viewing[1] < total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_COLLAPSE_RX = re.compile(r"\s+")


def _collapse(text: str | None) -> str:
    if text is None:
        return ""
    return _COLLAPSE_RX.sub(" ", text).strip()
