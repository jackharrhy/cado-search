"""FastAPI app exposing the CADO search UI and MCP endpoint.

The UI is intentionally plain: separate company and lobbyist search pages,
HTMX-powered live result tables, and clean detail URLs for each record. No JS
framework or build step, just FastAPI + Jinja2 + HTMX.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings

from ..mcp import create_mcp_server
from ..query import (
    CompanySearchFilters,
    CompanySortField,
    LobbyistSearchFilters,
    LobbyistSortField,
    RegistryQueryService,
    SortDirection,
)
from ..settings import settings
from ..snapshot import SnapshotValidationError, validate_database

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
                f"DuckDB at {resolved_path} does not exist yet. Run `cado refresh` first."
            )
        try:
            validate_database(resolved_path)
        except SnapshotValidationError as exc:
            raise RuntimeError(f"DuckDB snapshot is not ready: {exc}") from exc
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
    templates.env.filters["human_date"] = _human_date
    app.state.templates = templates
    app.state.query_service = service
    app.state.mcp = mcp

    static_dir = _static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def get_service(request: Request) -> RegistryQueryService:
        return request.app.state.query_service  # type: ignore[no-any-return]

    # ---- pages ---------------------------------------------------------

    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> dict[str, str]:
        manifest = validate_database(resolved_path)
        return {"status": "ok", "snapshot_id": manifest.snapshot_id}

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        context = _registry_context(query_service)
        context["company_locations"] = query_service.get_company_location_options()
        return templates.TemplateResponse(
            request,
            "index.html",
            context,
        )

    @app.get("/lobbyists", response_class=HTMLResponse)
    def lobbyists(
        request: Request,
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        context = _registry_context(query_service)
        return templates.TemplateResponse(
            request,
            "lobbyists.html",
            context,
        )

    # ---- search endpoint (HTMX target) --------------------------------

    @app.get("/search/companies", response_class=HTMLResponse)
    def search_companies(
        request: Request,
        q: str = Query(
            "",
            max_length=200,
            description="Company name, number, current director, or previous name",
        ),
        name: str = Query("", max_length=200, description="Current company name substring"),
        number: str = Query("", max_length=32, description="Exact company number"),
        corp_type: str = Query("", description="Filter by corporation_type"),
        status: str = Query("", description="Filter by status"),
        category: str = Query("", description="Filter by category"),
        incorporated_from: str = Query("", max_length=10),
        incorporated_to: str = Query("", max_length=10),
        city: str = Query("", max_length=200),
        province_state: str = Query("", max_length=200),
        sort: CompanySortField | Literal[""] = Query(""),
        direction: SortDirection = Query("asc"),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        incorporation_start = _optional_date(incorporated_from, "incorporated_from")
        incorporation_end = _optional_date(incorporated_to, "incorporated_to")
        page = query_service.search_companies(
            query=q,
            filters=CompanySearchFilters.model_validate(
                {
                    "current_names": {"terms": [name]} if name else None,
                    "company_numbers": [number] if number else None,
                    "corporation_types": [corp_type] if corp_type else None,
                    "statuses": [status] if status else None,
                    "categories": [category] if category else None,
                    "incorporation_date": (
                        {"date_from": incorporation_start, "date_to": incorporation_end}
                        if incorporation_start or incorporation_end
                        else None
                    ),
                    "registered_office": (
                        {
                            "city": city or None,
                            "province_state": province_state or None,
                        }
                        if city or province_state
                        else None
                    ),
                }
            ),
            sort_by=sort or None,
            sort_direction=direction,
            limit=limit,
        )
        return templates.TemplateResponse(
            request,
            "_company_results.html",
            {"rows": page.items, "total": page.total, "limit": limit, "q": q},
        )

    @app.get("/search/lobbyists", response_class=HTMLResponse)
    def search_lobbyists(
        request: Request,
        q: str = Query("", max_length=200),
        registration_number: str = Query("", max_length=64),
        contact_name: str = Query("", max_length=200),
        firm_name: str = Query("", max_length=200),
        lobbyist_type: str = Query(""),
        status: str = Query("", max_length=100),
        effective_from: str = Query("", max_length=10),
        effective_to: str = Query("", max_length=10),
        sort: LobbyistSortField | Literal[""] = Query(""),
        direction: SortDirection = Query("asc"),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        effective_start = _optional_date(effective_from, "effective_from")
        effective_end = _optional_date(effective_to, "effective_to")
        page = query_service.search_lobbyists(
            query=q,
            filters=LobbyistSearchFilters.model_validate(
                {
                    "registration_numbers": (
                        [registration_number] if registration_number else None
                    ),
                    "contact_names": {"terms": [contact_name]} if contact_name else None,
                    "firm_names": {"terms": [firm_name]} if firm_name else None,
                    "lobbyist_types": [lobbyist_type] if lobbyist_type else None,
                    "statuses": [status] if status else None,
                    "effective_date": (
                        {"date_from": effective_start, "date_to": effective_end}
                        if effective_start or effective_end
                        else None
                    ),
                }
            ),
            sort_by=sort or None,
            sort_direction=direction,
            limit=limit,
        )
        return templates.TemplateResponse(
            request,
            "_lobbyist_results.html",
            {"rows": page.items, "total": page.total, "limit": limit, "q": q},
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
            raise HTTPException(status_code=404, detail="Company record not found")
        return templates.TemplateResponse(
            request,
            "company_detail.html",
            {
                "c": company,
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
            raise HTTPException(status_code=404, detail="Lobbyist registration not found")
        return templates.TemplateResponse(
            request,
            "lobbyist_detail.html",
            {"r": registration},
        )

    # Keep the MCP application's built-in /mcp route exact. This catch-all
    # mount must remain last so the HTML, static, and documentation routes win.
    app.mount("/", mcp_app, name="mcp")
    return app


def _registry_context(query_service: RegistryQueryService) -> dict[str, object]:
    status = query_service.get_dataset_status()
    return {
        "counts": {
            "companies": status.companies.count,
            "condominiums": status.condominiums.count,
            "cooperatives": status.cooperatives.count,
            "lobbyists": status.lobbyists.count,
        },
        "snapshot": status,
    }


def _optional_date(value: str, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be a valid YYYY-MM-DD date"
        ) from exc


def _human_date(value: datetime) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"
