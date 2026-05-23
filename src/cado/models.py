"""Pydantic data models for CADO registry records.

These are the *parsed* domain types. Raw HTML is cached separately so we can
re-derive these whenever the parser changes.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorporationType(StrEnum):
    """The discriminator that decides which registry a record belongs to.

    Empirically the upstream site uses these exact strings in ``lblCorporationType``.
    """

    COMPANY = "Company"
    CONDOMINIUM = "Condominium"
    COOPERATIVE = "Co-operative"


class Category(StrEnum):
    """``lblCategory`` value. "Local" = NL-incorporated, "Extra-Provincial" =
    registered to do business in NL but incorporated elsewhere."""

    LOCAL = "Local"
    EXTRA_PROVINCIAL = "Extra-Provincial"


class Address(BaseModel):
    """A postal address as rendered on a CADO details page.

    Fields are optional because the site frequently omits them; older records
    in particular are missing nearly everything except a province code.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    contact: str | None = None
    line1: str | None = None
    line2: str | None = None
    line3: str | None = None
    city: str | None = None
    province_state: str | None = None
    country: str | None = None
    postal_zip: str | None = None

    @model_validator(mode="after")
    def _coerce_blanks(self) -> Self:
        # Empty strings become None so equality / "is populated" checks are easy.
        for fname, val in list(self.__dict__.items()):
            if val == "":
                setattr(self, fname, None)
        return self

    @property
    def is_empty(self) -> bool:
        return all(v is None for v in self.__dict__.values())


class Director(BaseModel):
    """A current director as listed on a company's detail page.

    The upstream renders "First<br>Last" with awkward whitespace; the parser
    normalises both into ``full_name`` and exposes the split components when
    they're cleanly identifiable.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str
    first_name: str | None = None
    last_name: str | None = None


class PreviousName(BaseModel):
    """An historical name a company / condo / co-op used to operate under.

    These are rendered as their own table rows in ``Previous Names``. The
    upstream may include only the previous string and the date.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    effective_date: date | None = None


class Company(BaseModel):
    """A row from the Companies / Condominiums / Co-operatives registries.

    The three registries share infrastructure on the upstream site and the
    same id space (verified empirically); the ``corporation_type`` field is
    the discriminator.

    .. note::
        ``number`` is a *string*, not an integer. While the bulk of records
        carry a plain numeric id (``"25166"``, ``"99000"``), legacy and
        extra-provincial filings use a digit + uppercase-letter suffix
        scheme (``"2D"``, ``"3CM"``, ``"100M"``). The upstream uses these
        as the canonical primary key.
    """

    model_config = ConfigDict(str_strip_whitespace=True, use_enum_values=False)

    # Identity
    number: str = Field(description="lblCompanyNumber, e.g. '25166', '2D', '3CM'")
    name: str = Field(description="lblCompanyName")
    corporation_type: CorporationType
    category: Category | None = None
    status: str | None = Field(default=None, description="raw lblStatus text, e.g. 'Active'")

    # Dates
    incorporation_date: date | None = None
    registration_date: date | None = Field(
        default=None,
        description="lblRegistrationDate: only set for extra-provincial registrants",
    )
    last_annual_return: date | None = None

    # Classification
    business_type: str | None = None
    incorporation_jurisdiction: str | None = None
    filing_type: str | None = None
    min_max_directors: str | None = Field(default=None, description="raw 'min / max' string")

    # Free-form notes
    additional_info: str | None = None
    historical_remarks: list[str] = Field(default_factory=list)

    # Addresses
    registered_office: Address = Field(default_factory=Address)
    mailing_address: Address = Field(default_factory=Address)
    mailing_same_as_registered: bool = False

    # Relations
    directors: list[Director] = Field(default_factory=list)
    previous_names: list[PreviousName] = Field(default_factory=list)


class CompanySearchHit(BaseModel):
    """A single row from a number-or-keyword search result list.

    Carries enough information to (a) display in a UI and (b) reconstruct the
    postback needed to drill into the underlying detail page.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    number: str = Field(description="full id, possibly with suffix like '2D', '100CM'")
    row_index: int = Field(description="1-based ``_ctlN`` index used for __doPostBack drill-in")
    status: str
    corporation_type: str
    date_text: str | None = None  # raw upstream date, kept as string for now


class CompanySearchResult(BaseModel):
    """The page returned by a name/number search when it doesn't 302."""

    total: int
    viewing: str  # e.g. "1-10"
    hits: list[CompanySearchHit]
