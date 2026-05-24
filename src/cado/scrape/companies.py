"""Bulk scraper for the Companies / Condominiums / Co-operatives registry.

Enumerates every integer ``N`` in a range and POSTs ``txtCompanyNumber=N`` to
the upstream search form. The upstream responds in one of three ways:

* **exact-match singleton** — 302 to ``CompanyDetails.aspx``: we save the
  detail HTML and move on. This is the fast path (one POST per record).
* **multi-row list** — for low/legacy numbers (e.g. ``1``, ``100``) several
  records share a digit prefix but use uppercase-letter suffixes
  (``"1I"``, ``"3CM"``). We save the list page, then *postback-drill* into
  each row to fetch every suffixed detail page individually.
* **empty** — the form re-renders with no result rows. Recorded in the
  scrape log as a "miss" so we don't retry on the next run.

The scraper keeps a separate ``CADOClient`` per worker so their server-side
session state can't interleave. A shared :class:`~cado.http.RateLimiter` and
:class:`asyncio.Semaphore` enforce the global cap across all workers.

Resumption is "natural": if ``HtmlCache.exists(N)`` then skip; the scrape log
adds an authoritative "we tried N and got nothing" record so misses aren't
retried either.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..http import CADOClient, RateLimiter
from ..parsers import extract_company_number, parse_company_search_results
from ..settings import settings
from ..storage import HtmlCache

log = logging.getLogger(__name__)

SEARCH_URL = "/Company/CompanyNameNumberSearch.aspx"

# The empirical upper bound found during reconnaissance was around 100600.
# We sweep to 105000 by default to give the registry a few hundred records of
# headroom — overshoot just produces cheap "empty" rows in the log.
DEFAULT_MAX_NUMBER = 105_000


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScrapeOutcome:
    """The result of trying to scrape a single integer search number."""

    number: int
    kind: str  # "detail" | "hits" | "empty" | "cached" | "error"
    ids_saved: list[str] = field(default_factory=list)
    error: str | None = None
    attempted_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class ScrapeStats:
    """Running counters for a scrape session."""

    attempted: int = 0
    details: int = 0
    multi_hit_pages: int = 0
    suffixed_drilled: int = 0
    empty: int = 0
    cached: int = 0
    errors: int = 0

    def record(self, outcome: ScrapeOutcome) -> None:
        self.attempted += 1
        if outcome.kind == "detail":
            self.details += 1
        elif outcome.kind == "hits":
            self.multi_hit_pages += 1
            # ``ids_saved`` minus the list page itself
            self.suffixed_drilled += max(0, len(outcome.ids_saved) - 1)
        elif outcome.kind == "empty":
            self.empty += 1
        elif outcome.kind == "cached":
            self.cached += 1
        else:
            self.errors += 1


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class CompanyScraper:
    """Drives a multi-worker scrape over a range of search numbers.

    Use as an async context manager so worker clients are closed cleanly even
    on cancellation.
    """

    def __init__(
        self,
        *,
        cache: HtmlCache | None = None,
        concurrency: int | None = None,
        rate_per_second: float | None = None,
        skip_cached: bool = True,
        error_log_path: Path | None = None,
    ) -> None:
        self.cache = cache or HtmlCache(registry="companies")
        self.concurrency = concurrency or settings.max_concurrency
        self.rate_per_second = rate_per_second or settings.requests_per_second
        self.skip_cached = skip_cached
        # Where to append per-record error rows (one JSON object per line).
        # Default: <data_dir>/scrape_errors_companies.jsonl
        self.error_log_path = error_log_path or (
            settings.data_dir / "scrape_errors_companies.jsonl"
        )

        # Shared across all workers.
        self._limiter = RateLimiter(self.rate_per_second)
        self._semaphore = asyncio.Semaphore(self.concurrency)

        self._stack: AsyncExitStack | None = None
        self._clients: list[CADOClient] = []

    # ---- lifecycle ----------------------------------------------------

    async def __aenter__(self) -> CompanyScraper:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for _ in range(self.concurrency):
            client = CADOClient(limiter=self._limiter, semaphore=self._semaphore)
            await self._stack.enter_async_context(client)
            self._clients.append(client)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)
            self._stack = None
        self._clients.clear()

    # ---- public API ---------------------------------------------------

    def scrape_range(
        self,
        start: int = 1,
        stop: int = DEFAULT_MAX_NUMBER,
    ) -> AsyncIterator[ScrapeOutcome]:
        """Scrape every number in ``[start, stop)``, yielding outcomes."""
        return self.scrape_numbers(range(start, stop))

    async def scrape_numbers(self, numbers: Iterable[int]) -> AsyncIterator[ScrapeOutcome]:
        """Scrape an arbitrary collection of numbers (e.g. a retry list).

        Outcomes are yielded as workers complete them, so ordering is not
        guaranteed. Stop iteration to cancel cleanly — the ``__aexit__`` on
        the enclosing context manager will close any in-flight clients.
        """
        if not self._clients:
            raise RuntimeError("CompanyScraper must be used as an async context manager")

        # ``int`` items, with ``None`` as the per-worker shutdown sentinel.
        queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=self.concurrency * 8)
        # ``ScrapeOutcome`` items, with ``None`` pushed once *all* workers
        # have exited to signal the iteration is over.
        outcomes: asyncio.Queue[ScrapeOutcome | None] = asyncio.Queue()
        n_workers = len(self._clients)

        async def producer() -> None:
            for n in numbers:
                await queue.put(n)
            for _ in range(n_workers):
                await queue.put(None)

        async def worker(client: CADOClient) -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                try:
                    outcome = await self._scrape_one(client, item)
                except Exception as exc:
                    log.exception("worker error on number=%s", item)
                    outcome = ScrapeOutcome(
                        number=item,
                        kind="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self._append_error_log(outcome)
                await outcomes.put(outcome)

        async def workforce() -> None:
            await asyncio.gather(*workers, return_exceptions=False)
            await outcomes.put(None)  # final sentinel

        prod = asyncio.create_task(producer(), name="cado-producer")
        workers = [
            asyncio.create_task(worker(c), name=f"cado-worker-{i}")
            for i, c in enumerate(self._clients)
        ]
        watcher = asyncio.create_task(workforce(), name="cado-watcher")

        try:
            while True:
                outcome = await outcomes.get()
                if outcome is None:
                    break
                yield outcome
        finally:
            for t in (prod, watcher, *workers):
                if not t.done():
                    t.cancel()
            await asyncio.gather(prod, watcher, *workers, return_exceptions=True)

    # ---- error log ---------------------------------------------------

    def _append_error_log(self, outcome: ScrapeOutcome) -> None:
        """Persist a per-record error to ``self.error_log_path`` (JSONL)."""
        self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "number": outcome.number,
            "error": outcome.error,
            "at": outcome.attempted_at.isoformat(),
        }
        with self.error_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    @classmethod
    def read_error_log(cls, path: Path | None = None) -> list[int]:
        """Return the deduplicated list of company numbers from an error log."""
        path = path or (settings.data_dir / "scrape_errors_companies.jsonl")
        if not path.exists():
            return []
        seen: set[int] = set()
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    n = json.loads(stripped)["number"]
                except (ValueError, KeyError):
                    continue
                seen.add(int(n))
        return sorted(seen)

    # ---- per-record logic ---------------------------------------------

    async def _scrape_one(self, client: CADOClient, number: int) -> ScrapeOutcome:
        """Scrape a single search number, drilling into suffixed rows as needed.

        Classification is done by the URL the upstream redirected us to, not
        by parsing the body:

        * ``CompanyDetails.aspx``                -> singleton (detail kind)
        * ``CompanyNameNumberSearch.aspx`` + a result table -> multi-row (hits)
        * ``CompanyNameNumberSearch.aspx`` + no table       -> miss (empty)

        We *always* save the response HTML when the upstream gave us a detail
        page, even if the body is malformed (e.g. an empty ``lblCompanyName``,
        which the upstream produces ~0.05% of the time for unknown reasons).
        Re-parsing happens at ingest time and any record that's still bad
        ends up in the ``ingest_log`` table with ``parsed_ok=false``.
        """
        key = str(number)

        if self.skip_cached and (
            self.cache.exists(key, kind="detail") or self.cache.exists(key, kind="list")
        ):
            log.debug("skip cached number=%s", number)
            return ScrapeOutcome(number=number, kind="cached")

        # Step 1: GET the form to refresh viewstate.
        await client.get(SEARCH_URL)

        # Step 2: POST txtCompanyNumber=N.
        response = await client.post_back(
            SEARCH_URL,
            extra_fields={
                "txtNameKeywords1": "",
                "txtNameKeywords2": "",
                "txtCompanyNumber": key,
            },
            button=("btnSearch", "10"),
        )
        final_url = str(response.url)

        if "CompanyDetails.aspx" in final_url:
            # Singleton path. The upstream's canonical id may differ from the
            # search number (e.g. searching '2D' resolves to itself, but a
            # suffix-less search for '2' lands on '2D' on some legacy paths).
            # Best-effort extract; fall back to the search number.
            saved_id = extract_company_number(response.text) or key
            self.cache.write(saved_id, response.text, kind="detail")
            return ScrapeOutcome(number=number, kind="detail", ids_saved=[saved_id])

        # Otherwise we're back on the search form; figure out which sub-case.
        hits = parse_company_search_results(response.text)
        if not hits.hits:
            return ScrapeOutcome(number=number, kind="empty")

        # Multi-row: save the list page, then drill into each suffixed row.
        list_html = response.text
        self.cache.write(key, list_html, kind="list")
        saved_ids: list[str] = [key + "[list]"]

        # The viewstate captured on the result-list page is reusable for every
        # row drill from within this session. Reset to it before each drill so
        # a prior drill's response (which came from CompanyDetails.aspx)
        # doesn't bleed in -- otherwise the second drill 302s to ErrorPage.
        list_viewstate = client.last_viewstate
        for hit in hits.hits:
            if list_viewstate is not None:
                client._last_viewstate = list_viewstate
            drill_resp = await client.post_back(
                SEARCH_URL,
                event_target=f"rptCompanyNameSearchResults$_ctl{hit.row_index}$lbtCompanyNumber",
                extra_fields={
                    "txtNameKeywords1": "",
                    "txtNameKeywords2": "",
                    "txtCompanyNumber": key,
                },
            )
            if "CompanyDetails.aspx" not in str(drill_resp.url):
                log.warning(
                    "drill %s[_ctl%s] did not land on details (url=%s)",
                    key,
                    hit.row_index,
                    drill_resp.url,
                )
                continue
            # Trust the hit's number rather than re-extracting; this matches
            # the search row's reported id and is correct even if the detail
            # page has an empty lblCompanyNumber.
            self.cache.write(hit.number, drill_resp.text, kind="detail")
            saved_ids.append(hit.number)

        return ScrapeOutcome(number=number, kind="hits", ids_saved=saved_ids)
