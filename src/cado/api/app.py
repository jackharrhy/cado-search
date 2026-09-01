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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings

from ..mcp import create_mcp_server
from ..query import CompanySearchFilters, RegistryQueryService
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
        status = query_service.get_dataset_status()
        counts = {
            "companies": status.companies.count,
            "condominiums": status.condominiums.count,
            "cooperatives": status.cooperatives.count,
            "lobbyists": status.lobbyists.count,
        }
        return templates.TemplateResponse(
            request,
            "index.html",
            {"counts": counts, "snapshot": status},
        )

    # ---- search endpoint (HTMX target) --------------------------------

    @app.get("/search/companies", response_class=HTMLResponse)
    def search_companies(
        request: Request,
        q: str = Query("", description="Company name, number, current director, or previous name"),
        corp_type: str = Query("", description="Filter by corporation_type"),
        status: str = Query("", description="Filter by status"),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        page = query_service.search_companies(
            query=q,
            filters=CompanySearchFilters.model_validate(
                {
                    "corporation_types": [corp_type] if corp_type else None,
                    "statuses": [status] if status else None,
                }
            ),
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
        q: str = Query(""),
        limit: int = Query(50, ge=1, le=50),
        query_service: RegistryQueryService = Depends(get_service),
    ) -> HTMLResponse:
        page = query_service.search_lobbyists(query=q, limit=limit)
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
