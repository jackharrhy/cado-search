"""Model Context Protocol adapter for the read-only CADO query service."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .query import (
    CompanyRecord,
    CompanySearchFilters,
    CompanySearchPage,
    DatasetStatus,
    LobbyistRecord,
    LobbyistSearchFilters,
    LobbyistSearchPage,
    RegistryQueryService,
)
from .settings import settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

CompanyQuery = Annotated[
    str,
    Field(
        max_length=200,
        description=(
            "Optional case-insensitive literal substring searched across the current company "
            "name, current director names, and previous names, or an exact company number. "
            "Results say which fields matched. Use filters for multiple people or constraints."
        ),
        examples=["Jack Harrhy", "Softspark", "99837"],
    ),
]
LobbyistQuery = Annotated[
    str,
    Field(
        max_length=200,
        description=(
            "Optional case-insensitive literal substring searched across the contact, firm, "
            "client, and in-house lobbyist names, or an exact registration number. Results "
            "say which fields matched."
        ),
        examples=["Atlantic", "Rhonda Tulk-Lane", "IHL-867-1005"],
    ),
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
            "site for legal or time-sensitive use. Start with a search tool, then use its "
            "record URL or corresponding get tool for complete details. The free-text query "
            "searches identity fields and returns query_matches explaining each hit. Use the "
            "typed filters object for precise or multi-value searches: different filter fields "
            "are ANDed, registry-value lists accept any listed value, and text filters explicitly "
            "choose match='any' or match='all'. Search tools return bounded pages; use "
            "next_offset to continue. Registry roles must be reported literally: a current "
            "director or lobbyist is not evidence that someone founded or owns an organization. "
            "All tools are read-only."
        ),
        website_url=settings.public_base_url,
        version=__version__,
    )

    @server.tool(title="Search companies", annotations=READ_ONLY)
    def search_companies(
        query: CompanyQuery = "",
        filters: Annotated[
            CompanySearchFilters | None,
            Field(
                description=(
                    "Optional structured filters. Different fields are ANDed. For example, to "
                    "find a company listing both people as current directors, pass "
                    "{'director_names': {'terms': ['Jack Harrhy', 'Martin Whelan'], "
                    "'match': 'all'}}."
                ),
                examples=[
                    {
                        "director_names": {
                            "terms": ["Jack Harrhy", "Martin Whelan"],
                            "match": "all",
                        },
                        "statuses": ["Active"],
                    }
                ],
            ),
        ] = None,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> CompanySearchPage:
        """Search mirrored companies, condominiums, and co-operatives.

        Use ``query`` for one known company, person, previous name, or number.
        Use ``filters`` to combine registry facts, including multiple current
        directors with explicit any/all semantics, classifications, dates, and
        locations. Company numbers are strings and may have suffixes like ``2D``.
        Director matches describe the registry's current-director listing; they
        do not establish founders, owners, or historical directors.
        """
        return service.search_companies(
            query=query,
            filters=filters,
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
        query: LobbyistQuery = "",
        filters: Annotated[
            LobbyistSearchFilters | None,
            Field(
                description=(
                    "Optional structured filters. Different fields are ANDed; nested text and "
                    "activity filters state whether any or all terms are required."
                ),
                examples=[
                    {
                        "subject_matters": {
                            "terms": ["Economic Development"],
                            "match": "any",
                            "expects_to_lobby": True,
                        },
                        "statuses": ["Approved"],
                    }
                ],
            ),
        ] = None,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> LobbyistSearchPage:
        """Search mirrored lobbyist registrations.

        Use ``query`` for one known person, firm, client, or registration number.
        Use ``filters`` to combine people, organizations, subjects, targets,
        techniques, status, dates, and locations. Omit both to browse.
        """
        return service.search_lobbyists(
            query=query,
            filters=filters,
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
        """Get coverage, snapshot identity, provenance timestamps, and source attribution."""
        return service.get_dataset_status()

    return server
