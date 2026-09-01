"""MCP tool contract and Streamable HTTP endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server import MCPServer
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from cado.api import create_app
from cado.mcp import create_mcp_server
from cado.query import RegistryQueryService


@pytest.fixture
def mcp_server(seeded_db: Path) -> MCPServer[None]:
    service = RegistryQueryService(seeded_db, public_base_url="https://cado.example")
    return create_mcp_server(service)


async def test_lists_five_read_only_tools(mcp_server: MCPServer[None]) -> None:
    async with Client(mcp_server) as client:
        result = await client.list_tools()

    assert [tool.name for tool in result.tools] == [
        "search_companies",
        "get_company",
        "search_lobbyists",
        "get_lobbyist",
        "get_dataset_status",
    ]
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.output_schema is not None


async def test_search_schemas_teach_agents_the_nested_filter_contract(
    mcp_server: MCPServer[None],
) -> None:
    async with Client(mcp_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    company_schema = tools["search_companies"].input_schema
    company_defs = company_schema["$defs"]
    company_filters = company_defs["CompanySearchFilters"]
    director_filter = company_filters["properties"]["director_names"]
    text_terms = company_defs["TextTermsFilter"]
    assert "current director" in director_filter["description"].lower()
    assert director_filter["examples"][0]["match"] == "all"
    assert text_terms["properties"]["match"]["default"] == "any"
    assert company_schema["properties"]["filters"]["examples"][0]["statuses"] == ["Active"]

    lobbyist_schema = tools["search_lobbyists"].input_schema
    lobbyist_filters = lobbyist_schema["$defs"]["LobbyistSearchFilters"]
    activity = lobbyist_schema["$defs"]["LobbyingActivityFilter"]
    assert "client_names" in lobbyist_filters["properties"]
    assert "expects_to_lobby" in activity["properties"]


async def test_company_tools_return_structured_data(mcp_server: MCPServer[None]) -> None:
    async with Client(mcp_server) as client:
        search = await client.call_tool("search_companies", {"query": "Irving"})
        detail = await client.call_tool("get_company", {"number": "50000"})

    assert search.is_error is False
    assert search.structured_content is not None
    assert search.structured_content["items"][0]["number"] == "99000"
    assert detail.is_error is False
    assert detail.structured_content is not None
    assert len(detail.structured_content["directors"]) == 4
    assert detail.structured_content["record_url"] == "https://cado.example/company/50000"


async def test_company_search_accepts_multi_director_filters(
    mcp_server: MCPServer[None],
) -> None:
    async with Client(mcp_server) as client:
        search = await client.call_tool(
            "search_companies",
            {
                "filters": {
                    "director_names": {
                        "terms": ["Mark Courtney", "Steven Crewe"],
                        "match": "all",
                    },
                    "statuses": ["Active"],
                }
            },
        )

    assert search.is_error is False
    assert search.structured_content is not None
    assert search.structured_content["total"] == 1
    assert search.structured_content["items"][0]["number"] == "50000"


async def test_affiliated_person_search_returns_match_evidence(
    mcp_server: MCPServer[None],
) -> None:
    async with Client(mcp_server) as client:
        company = await client.call_tool("search_companies", {"query": "Mark Courtney"})
        lobbyist = await client.call_tool("search_lobbyists", {"query": "Tulk-Lane, Rhonda"})

    assert company.structured_content is not None
    assert company.structured_content["items"][0]["query_matches"] == [
        {"field": "current_director", "value": "Mark Courtney"}
    ]
    assert lobbyist.structured_content is not None
    assert lobbyist.structured_content["items"][0]["query_matches"] == [
        {"field": "in_house_lobbyist", "value": "Tulk-Lane, Rhonda"}
    ]


async def test_lobbyist_and_status_tools_return_structured_data(
    mcp_server: MCPServer[None],
) -> None:
    async with Client(mcp_server) as client:
        lobbyist = await client.call_tool("get_lobbyist", {"registration_number": "IHL-867-1005"})
        status = await client.call_tool("get_dataset_status")

    assert lobbyist.structured_content is not None
    assert lobbyist.structured_content["contact_name"] == "Rhonda Tulk-Lane"
    assert lobbyist.structured_content["subject_matters"][0]["name"] == "Economic Development"
    assert status.structured_content is not None
    assert status.structured_content["companies"]["count"] == 3
    assert status.structured_content["snapshot_id"] == "test-snapshot"


async def test_validation_and_not_found_are_tool_errors(
    mcp_server: MCPServer[None],
) -> None:
    async with Client(mcp_server) as client:
        invalid = await client.call_tool("search_companies", {"limit": 51})
        missing = await client.call_tool("get_company", {"number": "NOPE"})

    assert invalid.is_error is True
    assert missing.is_error is True
    assert missing.structured_content is None
    assert "No company" in missing.content[0].text


def _initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cado-contract-test", "version": "1.0"},
        },
    }


def test_streamable_http_is_mounted_at_exact_mcp_path(seeded_db: Path) -> None:
    with TestClient(create_app(seeded_db)) as client:
        response = client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={"accept": "application/json, text/event-stream"},
        )
        doubled = client.post(
            "/mcp/mcp",
            json=_initialize_payload(),
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "cado"
    assert doubled.status_code == 404


def test_streamable_http_rejects_unknown_hosts(seeded_db: Path) -> None:
    with TestClient(create_app(seeded_db)) as client:
        response = client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={
                "accept": "application/json, text/event-stream",
                "host": "attacker.example",
            },
        )

    assert response.status_code == 421
