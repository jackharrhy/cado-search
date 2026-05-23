"""FastAPI app exposing a HTMX-driven search UI over the CADO DuckDB.

The UI is intentionally plain: one main page with a search form (companies
and lobbyists), an HTMX-powered live result list, and clean detail URLs for
each record. No JS framework, no build step — just FastAPI + Jinja2 + HTMX.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import connect
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Open one shared read-only connection. DuckDB's read-only mode is
        # safe for cross-task reads.
        if not resolved_path.exists():
            raise RuntimeError(
                f"DuckDB at {resolved_path} does not exist yet. Run `cado ingest` first."
            )
        conn = connect(resolved_path, read_only=True)
        app.state.conn = conn
        try:
            yield
        finally:
            conn.close()

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

    static_dir = _static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def get_conn(request: Request) -> duckdb.DuckDBPyConnection:
        return request.app.state.conn  # type: ignore[no-any-return]

    # ---- pages ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        conn: duckdb.DuckDBPyConnection = Depends(get_conn),
    ) -> HTMLResponse:
        counts = {
            "companies": conn.execute(
                "SELECT COUNT(*) FROM companies WHERE corporation_type = 'Company'"
            ).fetchone()[0],
            "condominiums": conn.execute(
                "SELECT COUNT(*) FROM companies WHERE corporation_type = 'Condominium'"
            ).fetchone()[0],
            "cooperatives": conn.execute(
                "SELECT COUNT(*) FROM companies WHERE corporation_type = 'Co-operative'"
            ).fetchone()[0],
            "lobbyists": conn.execute("SELECT COUNT(*) FROM lobbyist_registrations").fetchone()[0],
        }
        return templates.TemplateResponse(request, "index.html", {"counts": counts})

    # ---- search endpoint (HTMX target) --------------------------------

    @app.get("/search/companies", response_class=HTMLResponse)
    async def search_companies(
        request: Request,
        q: str = Query("", description="Free text matched against company name"),
        corp_type: str = Query("", description="Filter by corporation_type"),
        status: str = Query("", description="Filter by status"),
        limit: int = Query(50, ge=1, le=500),
        conn: duckdb.DuckDBPyConnection = Depends(get_conn),
    ) -> HTMLResponse:
        clauses: list[str] = []
        params: list[object] = []
        if q.strip():
            clauses.append("(name ILIKE ? OR number = ?)")
            params.extend([f"%{q.strip()}%", q.strip()])
        if corp_type:
            clauses.append("corporation_type = ?")
            params.append(corp_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT number, name, corporation_type, status, category,
                   incorporation_date, ro_city, ro_province_state
            FROM companies
            {where}
            ORDER BY name
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM companies {where}", params[:-1]).fetchone()[0]
        return templates.TemplateResponse(
            request,
            "_company_results.html",
            {"rows": rows, "total": total, "limit": limit, "q": q},
        )

    @app.get("/search/lobbyists", response_class=HTMLResponse)
    async def search_lobbyists(
        request: Request,
        q: str = Query(""),
        limit: int = Query(50, ge=1, le=500),
        conn: duckdb.DuckDBPyConnection = Depends(get_conn),
    ) -> HTMLResponse:
        clauses: list[str] = []
        params: list[object] = []
        if q.strip():
            term = f"%{q.strip()}%"
            clauses.append("(contact_name ILIKE ? OR firm_name ILIKE ? OR registration_number = ?)")
            params.extend([term, term, q.strip()])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT registration_number, contact_name, firm_name, lobbyist_type,
                   status, effective_date
            FROM lobbyist_registrations
            {where}
            ORDER BY effective_date DESC NULLS LAST, contact_name
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM lobbyist_registrations {where}", params[:-1]
        ).fetchone()[0]
        return templates.TemplateResponse(
            request,
            "_lobbyist_results.html",
            {"rows": rows, "total": total, "limit": limit, "q": q},
        )

    # ---- detail pages -------------------------------------------------

    @app.get("/company/{number}", response_class=HTMLResponse)
    async def company_detail(
        request: Request,
        number: str,
        conn: duckdb.DuckDBPyConnection = Depends(get_conn),
    ) -> HTMLResponse:
        company = conn.execute("SELECT * FROM companies WHERE number = ?", [number]).fetchone()
        if company is None:
            return HTMLResponse(
                f"<h1>404</h1><p>No company with number {number!r}.</p>",
                status_code=404,
            )
        cols = [d[0] for d in conn.description]  # type: ignore[union-attr]
        company_dict = dict(zip(cols, company, strict=True))
        directors = conn.execute(
            "SELECT full_name FROM company_directors WHERE company_number = ? ORDER BY seq",
            [number],
        ).fetchall()
        previous_names = conn.execute(
            "SELECT name, effective_date FROM company_previous_names "
            "WHERE company_number = ? ORDER BY seq",
            [number],
        ).fetchall()
        remarks = conn.execute(
            "SELECT remark FROM company_historical_remarks WHERE company_number = ? ORDER BY seq",
            [number],
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "company_detail.html",
            {
                "c": company_dict,
                "directors": [d[0] for d in directors],
                "previous_names": previous_names,
                "remarks": [r[0] for r in remarks],
            },
        )

    @app.get("/lobbyist/{registration_number}", response_class=HTMLResponse)
    async def lobbyist_detail(
        request: Request,
        registration_number: str,
        conn: duckdb.DuckDBPyConnection = Depends(get_conn),
    ) -> HTMLResponse:
        row = conn.execute(
            "SELECT * FROM lobbyist_registrations WHERE registration_number = ?",
            [registration_number],
        ).fetchone()
        if row is None:
            return HTMLResponse(
                f"<h1>404</h1><p>No lobbyist with registration {registration_number!r}.</p>",
                status_code=404,
            )
        cols = [d[0] for d in conn.description]  # type: ignore[union-attr]
        reg_dict = dict(zip(cols, row, strict=True))
        return templates.TemplateResponse(
            request,
            "lobbyist_detail.html",
            {"r": reg_dict},
        )

    return app
