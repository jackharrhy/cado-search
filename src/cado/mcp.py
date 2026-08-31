"""Model Context Protocol adapter for the read-only CADO query service."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .models import Category, CorporationType, LobbyistType
from .query import (
    CompanyRecord,
    CompanySearchPage,
    DatasetStatus,
    LobbyistRecord,
    LobbyistSearchPage,
    RegistryQueryService,
)
from .settings import settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

Query = Annotated[
    str,
    Field(
        max_length=200,
        description="Case-insensitive name/firm text, or an exact registry number.",
    ),
]
Status = Annotated[
    str | None,
    Field(max_length=100, description="Case-insensitive exact registry status."),
]
Limit = Annotated[
    int,
    Field(ge=1, le=50, description="Maximum records to return; at most 50."),
]
Offset = Annotated[
    int,
    Field(ge=0, description="Zero-based result offset for pagination."),
]


def create_mcp_server(service: RegistryQueryService) -> MCPServer[None]:
    """Create an MCP server whose tools are backed by ``service``."""
    server: MCPServer[None] = MCPServer(
        name="cado",
        title="Newfoundland and Labrador CADO Search",
        description="Read-only search over a mirror of Newfoundland and Labrador's CADO registries.",
        instructions=(
            "Search the public Companies, Condominiums, Co-operatives, and Lobbyists "
            "registries for Newfoundland and Labrador. This server reads a mirror, "
            "never the live registry. Results can be stale and should be verified "
            "against the authoritative Government of Newfoundland and Labrador CADO "
            "site for legal or time-sensitive use. Search tools return bounded pages; "
            "use next_offset to continue. All tools are read-only."
        ),
        website_url=settings.public_base_url,
        version=__version__,
    )

    @server.tool(title="Search companies", annotations=READ_ONLY)
    def search_companies(
        query: Query = "",
        corporation_type: CorporationType | None = None,
        status: Status = None,
        category: Category | None = None,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> CompanySearchPage:
        """Search mirrored companies, condominiums, and co-operatives.

        ``query`` matches a substring of the current name or an exact company
        number. Company numbers are strings and can contain legacy suffixes
        such as ``2D``. Omit query to browse using filters.
        """
        return service.search_companies(
            query=query,
            corporation_type=corporation_type,
            status=status,
            category=category,
            limit=limit,
            offset=offset,
        )

    @server.tool(title="Get company record", annotations=READ_ONLY)
    def get_company(
        number: Annotated[
            str,
            Field(
                min_length=1,
                max_length=32,
                description="Exact company number, including any legacy letter suffix.",
            ),
        ],
    ) -> CompanyRecord:
        """Get one complete mirrored company, condominium, or co-operative record."""
        record = service.get_company(number)
        if record is None:
            raise ToolError(f"No company, condominium, or co-operative numbered {number!r}.")
        return record

    @server.tool(title="Search lobbyists", annotations=READ_ONLY)
    def search_lobbyists(
        query: Query = "",
        lobbyist_type: LobbyistType | None = None,
        status: Status = None,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> LobbyistSearchPage:
        """Search mirrored lobbyist registrations.

        ``query`` matches a substring of the contact or firm name, or an exact
        registration number such as ``IHL-867-1005``. Omit query to browse.
        """
        return service.search_lobbyists(
            query=query,
            lobbyist_type=lobbyist_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    @server.tool(title="Get lobbyist registration", annotations=READ_ONLY)
    def get_lobbyist(
        registration_number: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                description="Exact lobbyist registration number, such as IHL-867-1005.",
            ),
        ],
    ) -> LobbyistRecord:
        """Get one complete mirrored lobbyist registration."""
        record = service.get_lobbyist(registration_number)
        if record is None:
            raise ToolError(f"No lobbyist registration numbered {registration_number!r}.")
        return record

    @server.tool(title="Get dataset status", annotations=READ_ONLY)
    def get_dataset_status() -> DatasetStatus:
        """Get mirror coverage, newest ingestion timestamps, and source attribution."""
        return service.get_dataset_status()

    return server
