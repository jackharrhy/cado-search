"""FastAPI app exposing the CADO search UI and MCP endpoint.

The UI is intentionally plain: one main page with a search form (companies
and lobbyists), an HTMX-powered live result list, and clean detail URLs for
each record. No JS framework, no build step — just FastAPI + Jinja2 + HTMX.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings

from ..mcp import create_mcp_server
from ..query import CompanyRecord, LobbyistRecord, RegistryQueryService
from ..settings import settings

log = logging.getLogger(__name__)


def _template_dir() -> Path:
    return Path(str(resources.files("cado.api").joinpath("templates")))


def _static_dir() -> Path:
    return Path(str(resources.files("cado.api").joinpath("static")))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(db_path: Path | None = None) -> FastAPI:
    """Build a FastAPI instance bound to ``db_path`` (read-only)."""
    resolved_path = db_path or settings.duckdb_path
    service = RegistryQueryService(resolved_path)
    mcp = create_mcp_server(service)
    mcp_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not resolved_path.exists():
            raise RuntimeError(
                f"DuckDB at {resolved_path} does not exist yet. Run `cado ingest` first."
            )
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="CADO Search",
        description=(
            "Searchable mirror of the Government of Newfoundland and Labrador's "
            "public Companies / Condominiums / Co-operatives / Lobbyists registries."
        ),
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=_template_dir())
    app.state.templates = templates
    app.state.query_service = service
    app.state.mcp = mcp

    static_dir = _static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def get_service(request: Request) -> RegistryQueryService:
        return request.app.state.query_service  # type: ignore[no-any-return]

    # ---- pages ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        status = query_service.get_dataset_status()
        counts = {
            "companies": status.companies.count,
            "condominiums": status.condominiums.count,
            "cooperatives": status.cooperatives.count,
            "lobbyists": status.lobbyists.count,
        }
        return templates.TemplateResponse(request, "index.html", {"counts": counts})

    # ---- search endpoint (HTMX target) --------------------------------

    @app.get("/search/companies", response_class=HTMLResponse)
    def search_companies(
        request: Request,
        q: str = Query("", description="Free text matched against company name"),
        corp_type: str = Query("", description="Filter by corporation_type"),
        status: str = Query("", description="Filter by status"),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        page = query_service.search_companies(
            query=q,
            corporation_type=corp_type or None,
            status=status or None,
            limit=limit,
        )
        rows = [
            (
                item.number,
                item.name,
                item.corporation_type,
                item.status,
                item.category,
                item.incorporation_date,
                item.city,
                item.province_state,
            )
            for item in page.items
        ]
        return templates.TemplateResponse(
            request,
            "_company_results.html",
            {"rows": rows, "total": page.total, "limit": limit, "q": q},
        )

    @app.get("/search/lobbyists", response_class=HTMLResponse)
    def search_lobbyists(
        request: Request,
        q: str = Query(""),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        page = query_service.search_lobbyists(query=q, limit=limit)
        rows = [
            (
                item.registration_number,
                item.contact_name,
                item.firm_name,
                item.lobbyist_type,
                item.status,
                item.effective_date,
            )
            for item in page.items
        ]
        return templates.TemplateResponse(
            request,
            "_lobbyist_results.html",
            {"rows": rows, "total": page.total, "limit": limit, "q": q},
        )

    # ---- detail pages -------------------------------------------------

    @app.get("/company/{number}", response_class=HTMLResponse)
    def company_detail(
        request: Request,
        number: str,
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        company = query_service.get_company(number)
        if company is None:
            return HTMLResponse(
                f"<h1>404</h1><p>No company with number {number!r}.</p>",
                status_code=404,
            )
        company_dict = _company_template_record(company)
        return templates.TemplateResponse(
            request,
            "company_detail.html",
            {
                "c": company_dict,
                "directors": [director.full_name for director in company.directors],
                "previous_names": [
                    (previous.name, previous.effective_date) for previous in company.previous_names
                ],
                "remarks": company.historical_remarks,
            },
        )

    @app.get("/lobbyist/{registration_number}", response_class=HTMLResponse)
    def lobbyist_detail(
        request: Request,
        registration_number: str,
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        registration = query_service.get_lobbyist(registration_number)
        if registration is None:
            return HTMLResponse(
                f"<h1>404</h1><p>No lobbyist with registration {registration_number!r}.</p>",
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "lobbyist_detail.html",
            {"r": _lobbyist_template_record(registration)},
        )

    # Keep the MCP application's built-in /mcp route exact. This catch-all
    # mount must remain last so the HTML, static, and documentation routes win.
    app.mount("/", mcp_app, name="mcp")
    return app


def _company_template_record(company: CompanyRecord) -> dict[str, object]:
    data = company.model_dump()
    registered = data.pop("registered_office")
    mailing = data.pop("mailing_address")
    data.pop("mailing_same_as_registered")
    for key, value in registered.items():
        data[f"ro_{key}"] = value
    for key, value in mailing.items():
        data[f"ma_{key}"] = value
    data["ma_same_as_registered"] = company.mailing_same_as_registered
    return data


def _lobbyist_template_record(registration: LobbyistRecord) -> dict[str, object]:
    data = registration.model_dump()
    contact = data.pop("contact_address")
    firm = data.pop("firm_address")
    for key, value in contact.items():
        data[f"contact_{key}"] = value
    for key, value in firm.items():
        data[f"firm_{key}"] = value
    return data
