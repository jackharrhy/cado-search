# cado

A searchable DuckDB mirror of Newfoundland and Labrador's public
[CADO](https://cado.eservices.gov.nl.ca/) business and lobbyist records, with a
web UI and read-only, stateless MCP endpoint.

## Run locally

```bash
uv sync
uv run cado refresh  # first run takes roughly 2–3 hours
uv run cado serve
```

Open `http://127.0.0.1:8000`. MCP is at `http://127.0.0.1:8000/mcp`; run
`uv run cado info` to inspect the snapshot.

Refreshes resume after interruption. A new database is validated before it
replaces the current one, so a failed refresh does not affect the server.

## MCP

Tools: `search_companies`, `get_company`,
`search_lobbyists`, `get_lobbyist`, and `get_dataset_status`. Searches return
up to 50 records and use `next_offset` for pagination.

Public endpoint: `https://cado.jackharrhy.dev/mcp`. This is a periodically
refreshed mirror; check the government registry for legal or time-sensitive
work.

## Docker

Image: `ghcr.io/jackharrhy/cado-search`.

```bash
docker compose pull
docker compose run --rm refresh
docker compose up -d
```

The server mounts `/data` read-only. To update it, run:

```bash
docker compose run --rm refresh
docker compose run --rm cado check
```

For public hosting, put port 8000 behind TLS. Set `CADO_PUBLIC_BASE_URL`,
`CADO_MCP_ALLOWED_HOSTS`, and `CADO_MCP_ALLOWED_ORIGINS`, and back up `/data`.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
CADO_LIVE_TESTS=1 uv run pytest
```

## License

The data is © Government of Newfoundland and Labrador. The code is available
under the [MIT License](LICENSE).
