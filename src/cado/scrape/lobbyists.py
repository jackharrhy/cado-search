"""Bulk scraper for the Registry of Lobbyists.

The lobbyist registry is small (low hundreds of records) and exposes a
"Search All Registrations" button that lists every record paginated 10 at
a time. We scrape it in two passes:

1. **Index pass** — walk every page of the Search All results, capturing
   the ``(registration_number, page_no, row_index)`` of every record.
   This is cheap (~10s of requests) and gives us a stable work list.
2. **Detail pass** — for each indexed entry, re-walk back to its page and
   postback-drill into the row to fetch its ``lobbySummary.aspx``. The
   detail page is cached on disk under the registration number.

Single-threaded by design: pagination is server-stateful (cookies +
viewstate), so a single sequential worker is both correct and quick enough
to dump the entire registry in a few minutes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..http import CADOClient, RateLimiter
from ..models import LobbyistSearchHit
from ..parsers.lobbyist import (
    get_total_records,
    has_next_page,
    parse_lobbyist_search_results,
)
from ..settings import settings
from ..storage import HtmlCache

log = logging.getLogger(__name__)

SEARCH_URL = "/Lobbyist/LobbyistSearch.aspx"

_SEARCH_ALL_FIELDS = {
    "rdoLobbyistType": "rbTypeBoth",
    "rdoStatus": "rbStatusBoth",
}


@dataclass(slots=True, frozen=True)
class LobbyistIndexEntry:
    """A ``(page, row)`` coordinate we can postback-drill into to get a detail."""

    registration_number: str
    page_no: int
    row_index: int


@dataclass(slots=True)
class LobbyistOutcome:
    """The result of attempting to fetch one record's detail page."""

    registration_number: str
    kind: str  # "detail" | "skipped" | "error"
    error: str | None = None
    attempted_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class LobbyistScraper:
    """Single-worker lobbyist registry scraper."""

    def __init__(
        self,
        *,
        cache: HtmlCache | None = None,
        rate_per_second: float | None = None,
        skip_cached: bool = True,
    ) -> None:
        self.cache = cache or HtmlCache(registry="lobbyists")
        self.rate_per_second = rate_per_second or settings.requests_per_second
        self.skip_cached = skip_cached
        self._limiter = RateLimiter(self.rate_per_second)
        self._client: CADOClient | None = None

    # ---- lifecycle ----------------------------------------------------

    async def __aenter__(self) -> LobbyistScraper:
        self._client = CADOClient(limiter=self._limiter)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    # ---- public API ---------------------------------------------------

    async def build_index(self) -> tuple[int | None, list[LobbyistIndexEntry]]:
        """Walk every page of Search All and return one entry per record."""
        client = self._require_client()

        await client.get(SEARCH_URL)
        page = await client.post_back(
            SEARCH_URL,
            extra_fields=_SEARCH_ALL_FIELDS,
            button=("btnSearchAll", "10"),
        )
        total = get_total_records(page.text)
        log.info("lobbyist registry: %s total records", total)

        entries: list[LobbyistIndexEntry] = []
        page_no = 1
        while True:
            hits: list[LobbyistSearchHit] = parse_lobbyist_search_results(page.text)
            for hit in hits:
                entries.append(
                    LobbyistIndexEntry(
                        registration_number=hit.registration_number,
                        page_no=page_no,
                        row_index=hit.row_index,
                    )
                )
            if not has_next_page(page.text):
                break
            page = await client.post_back(
                SEARCH_URL,
                event_target="lbtNext",
                extra_fields=_SEARCH_ALL_FIELDS,
            )
            page_no += 1

        return total, entries

    async def scrape_details(
        self, entries: list[LobbyistIndexEntry]
    ) -> AsyncIterator[LobbyistOutcome]:
        """Fetch a ``lobbySummary.aspx`` for each indexed entry.

        Entries are processed in their original order; the scraper walks
        forward through pagination, drilling into each row in turn. After
        a drill the session loses the result table, so we re-seek to the
        same page before drilling the next row on that page.
        """
        if not entries:
            return
        client = self._require_client()

        # Group entries by page so we can re-walk pagination once per page.
        by_page: dict[int, list[LobbyistIndexEntry]] = {}
        for entry in entries:
            by_page.setdefault(entry.page_no, []).append(entry)

        for page_no in sorted(by_page):
            page_entries = by_page[page_no]

            # Fast path: if every record on this page is already cached, we
            # don't need to seek at all. Avoids paying O(page_no) requests
            # just to walk past cached rows -- which used to dominate the
            # cost of a "refresh" run after a previous successful scrape.
            if self.skip_cached and all(
                self.cache.exists(e.registration_number) for e in page_entries
            ):
                for entry in page_entries:
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number, kind="skipped"
                    )
                continue

            # Re-establish the result list at ``page_no``.
            try:
                await self._seek_to_page(page_no)
            except Exception as exc:
                for entry in page_entries:
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number,
                        kind="error",
                        error=f"seek_to_page({page_no}) failed: {exc}",
                    )
                continue

            # Figure out the last index on this page that actually needs a
            # drill so we know whether the post-drill re-seek can be skipped.
            uncached_indices = [
                i
                for i, e in enumerate(page_entries)
                if not (self.skip_cached and self.cache.exists(e.registration_number))
            ]
            last_uncached = uncached_indices[-1] if uncached_indices else -1

            for i, entry in enumerate(page_entries):
                if self.skip_cached and self.cache.exists(entry.registration_number):
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number, kind="skipped"
                    )
                    continue
                try:
                    response = await client.post_back(
                        SEARCH_URL,
                        event_target=f"rptSearchResults$_ctl{entry.row_index}$lbtRegNum",
                        extra_fields=_SEARCH_ALL_FIELDS,
                    )
                    if "lobbySummary.aspx" not in str(response.url):
                        yield LobbyistOutcome(
                            registration_number=entry.registration_number,
                            kind="error",
                            error=f"unexpected redirect to {response.url}",
                        )
                        continue
                    self.cache.write(entry.registration_number, response.text)
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number, kind="detail"
                    )
                except Exception as exc:
                    log.exception(
                        "lobbyist drill failed for %s", entry.registration_number
                    )
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number,
                        kind="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                # Re-seek after drilling so the next *uncached* row on the
                # same page is reachable. Skip if there is no later uncached
                # row -- saves N requests every time we drill the last
                # uncached row before a tail of cached ones.
                if i < last_uncached:
                    try:
                        await self._seek_to_page(page_no)
                    except Exception:
                        log.exception("re-seek to page %d failed", page_no)
                        break

    async def scrape_all(self) -> AsyncIterator[LobbyistOutcome]:
        """Convenience: build the index then scrape every detail."""
        _, entries = await self.build_index()
        async for outcome in self.scrape_details(entries):
            yield outcome

    # ---- internals ----------------------------------------------------

    def _require_client(self) -> CADOClient:
        if self._client is None:
            raise RuntimeError("LobbyistScraper must be used as an async context manager")
        return self._client

    async def _seek_to_page(self, page_no: int) -> None:
        """GET the search form, click Search All, advance to ``page_no``."""
        client = self._require_client()
        await client.get(SEARCH_URL)
        await client.post_back(
            SEARCH_URL,
            extra_fields=_SEARCH_ALL_FIELDS,
            button=("btnSearchAll", "10"),
        )
        for _ in range(page_no - 1):
            await client.post_back(
                SEARCH_URL,
                event_target="lbtNext",
                extra_fields=_SEARCH_ALL_FIELDS,
            )
