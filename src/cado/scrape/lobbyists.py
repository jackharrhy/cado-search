"""Bulk scraper for the Registry of Lobbyists.

Two passes:

1. **Index pass** — walk every page of the Search All results, capturing
   the ``(registration_number, page_no, row_index)`` of every record.
   Single-session by necessity (pagination is server-stateful) but cheap
   (~25s for the full registry).
2. **Detail pass** — work is grouped by *page*. Each unit of work is one
   page; a worker walks its session to the assigned page, **captures the
   viewstate**, then drills every row on that page reusing that one
   captured viewstate. K such workers run in parallel.

The viewstate trick is the key win. Empirically (verified on production),
the viewstate captured at page N is reusable for as many drills as we
want from within the same session, even after a prior drill has navigated
the session to a detail page. The original "one worker per row" design
paid `~N Next clicks` per row; this design pays it once per page.

Rough cost model with ``concurrency=8`` over 73 pages of 10 rows:
    walk_time(page p) ≈ 0.5 + 0.25*p seconds
    drill_batch_time ≈ 0.5 + 0.5 per row, serial within a page
    total wall time ≈ slowest_page_chain_through_workers
                    ≈ ~4 minutes for the whole registry
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
        """Fetch ``lobbySummary.aspx`` for each entry.

        Entries are grouped by ``page_no``; each page becomes one unit of
        work handled by a single :class:`CADOClient`. The client walks to
        the assigned page, captures the page's viewstate, then issues
        ``ctl1``..``ctlN`` drill postbacks reusing that one viewstate.
        Up to ``self.concurrency`` page-workers run in parallel.

        Pages are dispatched newest-first (highest ``page_no`` first) so the
        longest walks start before the queue thins out — a worker that
        finishes early can grab a cheaper page next.
        """
        if not entries:
            return

        # Pre-filter cached entries; emit ``skipped`` outcomes for them and
        # never queue them as work.
        outcomes: asyncio.Queue[LobbyistOutcome | None] = asyncio.Queue()
        pages: dict[int, list[LobbyistIndexEntry]] = {}
        for entry in entries:
            if self.skip_cached and self.cache.exists(entry.registration_number):
                await outcomes.put(
                    LobbyistOutcome(registration_number=entry.registration_number, kind="skipped")
                )
                continue
            pages.setdefault(entry.page_no, []).append(entry)

        if not pages:
            while not outcomes.empty():
                outcome = outcomes.get_nowait()
                if outcome is not None:
                    yield outcome
            return

        # Process deep pages first; their walk dominates.
        page_queue: asyncio.Queue[int | None] = asyncio.Queue()
        for page_no in sorted(pages, reverse=True):
            page_queue.put_nowait(page_no)
        # One sentinel per worker so they all exit cleanly.
        for _ in range(self.concurrency):
            page_queue.put_nowait(None)

        async def page_worker() -> None:
            while True:
                page_no = await page_queue.get()
                if page_no is None:
                    return
                try:
                    async for outcome in self._fetch_page(page_no, pages[page_no]):
                        await outcomes.put(outcome)
                except Exception as exc:
                    log.exception("page worker crashed on page %d", page_no)
                    for entry in pages[page_no]:
                        await outcomes.put(
                            LobbyistOutcome(
                                registration_number=entry.registration_number,
                                kind="error",
                                error=(
                                    f"page_worker({page_no}) crashed: {type(exc).__name__}: {exc}"
                                ),
                            )
                        )

        async def supervisor() -> None:
            await asyncio.gather(*(page_worker() for _ in range(self.concurrency)))
            await outcomes.put(None)

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

    async def _fetch_page(
        self, page_no: int, entries: list[LobbyistIndexEntry]
    ) -> AsyncIterator[LobbyistOutcome]:
        """Walk one session to ``page_no``, then drill every entry on that page.

        The key insight: once we've POSTed our way to page N, the viewstate
        on that page is reusable for every row drill from within the same
        session, even after a previous drill has moved that session to a
        detail page. So the per-page cost is::

            1 GET + 1 SearchAll + (N-1) Next  +  K drills

        instead of the old::

            K * (1 GET + 1 SearchAll + (N-1) Next + 1 drill)
        """
        async with CADOClient(limiter=self._limiter, semaphore=self._semaphore) as client:
            await self._walk_to_page(client, page_no)
            page_viewstate = client.last_viewstate
            if page_viewstate is None:
                # Should never happen; the walk just hit an HTML form.
                for entry in entries:
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number,
                        kind="error",
                        error=f"no viewstate captured at page {page_no}",
                    )
                return

            for entry in entries:
                # Reset the client's stored viewstate to the page-N capture
                # before each drill so a previous drill's response (which
                # came from lobbySummary.aspx) doesn't bleed in.
                client._last_viewstate = page_viewstate
                try:
                    response = await client.post_back(
                        SEARCH_URL,
                        event_target=(f"rptSearchResults$_ctl{entry.row_index}$lbtRegNum"),
                        extra_fields=_SEARCH_ALL_FIELDS,
                    )
                except Exception as exc:
                    log.exception(
                        "drill failed for %s on page %d",
                        entry.registration_number,
                        page_no,
                    )
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number,
                        kind="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                if "lobbySummary.aspx" not in str(response.url):
                    yield LobbyistOutcome(
                        registration_number=entry.registration_number,
                        kind="error",
                        error=f"unexpected redirect to {response.url}",
                    )
                    continue
                self.cache.write(entry.registration_number, response.text)
                yield LobbyistOutcome(registration_number=entry.registration_number, kind="detail")

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
