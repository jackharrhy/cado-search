"""Async HTTP client tailored to CADO's ASP.NET WebForms quirks.

The CADO site uses classic ASP.NET WebForms: every page emits a ``__VIEWSTATE``
blob plus a matching ``__VIEWSTATEGENERATOR`` and (often) ``__EVENTVALIDATION``
token. To "click" a link on a result page, the browser issues a POST back to
the same URL with ``__EVENTTARGET`` set to the control id and all the existing
form fields preserved. The server uses session cookies to remember the current
search context (e.g. which company you "selected"), then redirects to a detail
page that reads that state.

To navigate the site programmatically we need to:

1. Maintain cookies across requests (``httpx.AsyncClient`` handles this).
2. Parse the three hidden state fields from each response.
3. Re-emit them on every subsequent POST, plus any user inputs.

This module centralises that work behind a small API.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .settings import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# View state extraction
# ---------------------------------------------------------------------------

# The three hidden fields are emitted as plain `<input>` tags. A tolerant regex
# is much faster than parsing the full page with bs4 — these tags appear near
# the top of the document and we run this on every single request.
_HIDDEN_RX = re.compile(
    r'<input[^>]*\bname="(__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION)"'
    r'[^>]*\bvalue="([^"]*)"',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ViewState:
    """The trio of hidden ASP.NET fields needed to round-trip a postback."""

    viewstate: str
    generator: str
    event_validation: str | None  # not every page has this

    @classmethod
    def parse(cls, html: str) -> ViewState:
        found: dict[str, str] = {}
        for match in _HIDDEN_RX.finditer(html):
            found[match.group(1).upper()] = match.group(2)
        try:
            return cls(
                viewstate=found["__VIEWSTATE"],
                generator=found["__VIEWSTATEGENERATOR"],
                event_validation=found.get("__EVENTVALIDATION"),
            )
        except KeyError as exc:
            missing = exc.args[0]
            raise ViewStateError(f"missing hidden form field {missing}") from exc

    def to_form(self) -> dict[str, str]:
        data = {
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.generator,
        }
        if self.event_validation is not None:
            data["__EVENTVALIDATION"] = self.event_validation
        return data


class ViewStateError(RuntimeError):
    """Raised when a response is missing the expected ASP.NET hidden fields."""


# ---------------------------------------------------------------------------
# Token-bucket-ish rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Minimal asyncio rate limiter.

    Enforces an average rate of ``rate`` events/second over a sliding window of
    one second. Implemented as a queue of timestamps so bursts of size
    ``rate`` are allowed but the long-run rate is bounded.
    """

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate_per_second
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        loop = asyncio.get_event_loop()
        async with self._lock:
            now = loop.time()
            wait_for = self._next_slot - now
            if wait_for > 0:
                # Schedule the next slot relative to when this one was claimed.
                self._next_slot += self._interval
            else:
                # We're behind schedule; reset to "now".
                self._next_slot = now + self._interval
                wait_for = 0.0
        if wait_for > 0:
            await asyncio.sleep(wait_for)


# ---------------------------------------------------------------------------
# CADO client
# ---------------------------------------------------------------------------


_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class CADOClient:
    """Stateful async HTTP client for cado.eservices.gov.nl.ca.

    Use as an async context manager. Each method that performs a postback
    keeps the latest view state internally so you can chain calls naturally::

        async with CADOClient() as c:
            await c.get("/Company/CompanyNameNumberSearch.aspx")
            results = await c.post_back(
                "/Company/CompanyNameNumberSearch.aspx",
                extra_fields={"txtNameKeywords1": "TIM"},
                button=("btnSearch.x", "10"),
            )
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        rate_per_second: float | None = None,
        concurrency: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.base_url,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.read_timeout,
                pool=settings.read_timeout,
            ),
            headers={"User-Agent": user_agent or settings.user_agent},
            follow_redirects=True,
            http2=False,  # the server is old; HTTP/1.1 is safer
        )
        self._limiter = RateLimiter(rate_per_second or settings.requests_per_second)
        self._sem = asyncio.Semaphore(concurrency or settings.max_concurrency)
        self._last_viewstate: ViewState | None = None

    # ---- lifecycle -----------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- request helpers ----------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(settings.retries),
            wait=wait_exponential_jitter(initial=0.5, max=30.0),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                async with self._sem:
                    await self._limiter.acquire()
                    response = await self._client.request(method, url, data=data, headers=headers)
                    response.raise_for_status()
                    return response
        raise RuntimeError("unreachable: tenacity always raises or returns")

    async def get(self, url: str) -> httpx.Response:
        log.debug("GET %s", url)
        response = await self._request("GET", url)
        self._maybe_capture_viewstate(response)
        return response

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        referer: str | None = None,
    ) -> httpx.Response:
        log.debug("POST %s", url)
        headers: dict[str, str] = {}
        if referer is not None:
            headers["Referer"] = self._absolute(referer)
        response = await self._request("POST", url, data=data, headers=headers)
        self._maybe_capture_viewstate(response)
        return response

    async def post_back(
        self,
        url: str,
        *,
        event_target: str = "",
        event_argument: str = "",
        extra_fields: Mapping[str, str] | None = None,
        button: tuple[str, str] | None = None,
        referer: str | None = None,
    ) -> httpx.Response:
        """Perform an ASP.NET ``__doPostBack`` against ``url``.

        Parameters
        ----------
        event_target / event_argument:
            The control id / argument passed to ``__doPostBack``. For an image
            button (``<input type=image>``) leave these empty and pass
            ``button=(name, "10")`` instead.
        extra_fields:
            Form inputs (text boxes, radios, etc.) to preserve in the post body.
        button:
            For image button submits, the ``(name, coordinate)`` pair. Image
            buttons send ``name.x=10&name.y=10`` instead of an ``__EVENTTARGET``.
        referer:
            Sent as the ``Referer`` header. Defaults to ``url``.
        """
        if self._last_viewstate is None:
            raise RuntimeError(
                "post_back called before any GET; fetch the form first to capture its __VIEWSTATE"
            )
        body: dict[str, str] = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": event_argument,
            **self._last_viewstate.to_form(),
        }
        if extra_fields:
            body.update(extra_fields)
        if button is not None:
            name, coord = button
            body[f"{name}.x"] = coord
            body[f"{name}.y"] = coord
        return await self.post(url, data=body, referer=referer or url)

    # ---- internals -----------------------------------------------------

    def _absolute(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return f"{settings.base_url}{url}"

    def _maybe_capture_viewstate(self, response: httpx.Response) -> None:
        ctype = response.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return
        try:
            self._last_viewstate = ViewState.parse(response.text)
        except ViewStateError:
            # Some endpoints (error pages, popups) don't have a form; that's fine.
            self._last_viewstate = None

    @property
    def last_viewstate(self) -> ViewState | None:
        return self._last_viewstate
