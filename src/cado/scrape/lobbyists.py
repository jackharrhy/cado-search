"""Bulk scraper for the Registry of Lobbyists.

Two passes:

1. **Index pass** — walk every page of the Search All results, capturing the
   ``(registration_number, page_no, row_index)`` of every record. Necessarily
   single-session because pagination is server-stateful, but cheap (~25s).
2. **Detail pass** — for each indexed entry, dispatch to a worker pool. Each
   worker is an *independent* CADOClient (its own cookies + viewstate) that
   walks to its assigned page, drills the assigned row, then dies and gets
   replaced by a fresh worker on the next entry.

The detail pass is the slow one. Empirically the upstream handles 12-16
concurrent sessions cleanly, and parallelising the walk-and-drill gives a
~5x speedup over the old re-seek-after-each-drill approach.
"""

from __future__ import annotations

import asyncio
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
    """Lobbyist registry scraper with a parallel detail-fetch pool.

    The ``__aenter__`` opens one *index* client used to enumerate pages
    sequentially. Detail fetches each spin up their own short-lived client
    so they don't share session cookies with the indexer or each other.

    All clients share a global :class:`~cado.http.RateLimiter` and
    :class:`asyncio.Semaphore` so the upstream isn't hammered.
    """

    def __init__(
        self,
        *,
        cache: HtmlCache | None = None,
        rate_per_second: float | None = None,
        concurrency: int | None = None,
        skip_cached: bool = True,
    ) -> None:
        self.cache = cache or HtmlCache(registry="lobbyists")
        self.rate_per_second = rate_per_second or settings.requests_per_second
        self.concurrency = concurrency or settings.max_concurrency
        self.skip_cached = skip_cached
        # Shared across the index client and every detail worker.
        self._limiter = RateLimiter(self.rate_per_second)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._index_client: CADOClient | None = None

    # ---- lifecycle ----------------------------------------------------

    async def __aenter__(self) -> LobbyistScraper:
        # The index client gets the shared limiter but NOT the shared
        # semaphore -- we don't want index walks to starve detail workers.
        self._index_client = CADOClient(limiter=self._limiter)
        await self._index_client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._index_client is not None:
            await self._index_client.__aexit__(*exc)
            self._index_client = None

    # ---- public API ---------------------------------------------------

    async def build_index(self) -> tuple[int | None, list[LobbyistIndexEntry]]:
        """Walk every page of Search All and return one entry per record."""
        client = self._require_index_client()

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
        """Fetch a ``lobbySummary.aspx`` for each entry, parallel across workers.

        Each entry is its own unit of work: a fresh client walks to the entry's
        page and drills the row. Workers share a global rate limit and
        semaphore so the total in-flight request count is bounded.

        Outcomes are yielded as workers complete, in arbitrary order. Sorting
        entries by page_no descending before calling this method gives a
        modest tail-latency win: the longest jobs (drilling rows on the last
        page) start first.
        """
        if not entries:
            return

        # Pre-filter cached entries so we don't even queue them as work.
        outcomes: asyncio.Queue[LobbyistOutcome | None] = asyncio.Queue()
        work: list[LobbyistIndexEntry] = []
        for entry in entries:
            if self.skip_cached and self.cache.exists(entry.registration_number):
                # Cache hits get yielded immediately, no network needed.
                await outcomes.put(
                    LobbyistOutcome(registration_number=entry.registration_number, kind="skipped")
                )
            else:
                work.append(entry)

        if not work:
            # Drain the queue of skipped entries and return.
            while not outcomes.empty():
                outcome = outcomes.get_nowait()
                if outcome is not None:
                    yield outcome
            return

        # Sort by page_no descending so deep-page work starts first; this
        # smooths the tail when concurrency < number_of_pages.
        work.sort(key=lambda e: -e.page_no)

        async def worker(entry: LobbyistIndexEntry) -> None:
            try:
                outcome = await self._fetch_one(entry)
            except Exception as exc:
                log.exception("lobbyist fetch failed for %s", entry.registration_number)
                outcome = LobbyistOutcome(
                    registration_number=entry.registration_number,
                    kind="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            await outcomes.put(outcome)

        async def supervisor() -> None:
            await asyncio.gather(*(worker(e) for e in work))
            await outcomes.put(None)  # sentinel

        supervisor_task = asyncio.create_task(supervisor(), name="lobbyist-supervisor")

        try:
            while True:
                outcome = await outcomes.get()
                if outcome is None:
                    break
                yield outcome
        finally:
            if not supervisor_task.done():
                supervisor_task.cancel()
            await asyncio.gather(supervisor_task, return_exceptions=True)

    async def scrape_all(self) -> AsyncIterator[LobbyistOutcome]:
        """Convenience: build the index then scrape every detail."""
        _, entries = await self.build_index()
        async for outcome in self.scrape_details(entries):
            yield outcome

    # ---- internals ----------------------------------------------------

    def _require_index_client(self) -> CADOClient:
        if self._index_client is None:
            raise RuntimeError("LobbyistScraper must be used as an async context manager")
        return self._index_client

    async def _fetch_one(self, entry: LobbyistIndexEntry) -> LobbyistOutcome:
        """Spin up a fresh client, walk to the entry's page, drill the row."""
        async with CADOClient(limiter=self._limiter, semaphore=self._semaphore) as client:
            await self._walk_to_page(client, entry.page_no)
            response = await client.post_back(
                SEARCH_URL,
                event_target=f"rptSearchResults$_ctl{entry.row_index}$lbtRegNum",
                extra_fields=_SEARCH_ALL_FIELDS,
            )
            if "lobbySummary.aspx" not in str(response.url):
                return LobbyistOutcome(
                    registration_number=entry.registration_number,
                    kind="error",
                    error=f"unexpected redirect to {response.url}",
                )
            self.cache.write(entry.registration_number, response.text)
            return LobbyistOutcome(registration_number=entry.registration_number, kind="detail")

    async def _walk_to_page(self, client: CADOClient, page_no: int) -> None:
        """Establish ``page_no`` of the Search All results in ``client``'s session."""
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
