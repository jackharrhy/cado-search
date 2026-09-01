# cado

A searchable DuckDB mirror of Newfoundland and Labrador's public
[CADO](https://cado.eservices.gov.nl.ca/) business and lobbyist records, with a
web UI and read-only, stateless MCP endpoint.

## Run locally

```bash
uv sync
uv run cado run  # first run takes roughly 2–3 hours
```

Open `http://127.0.0.1:8000`. MCP is at `http://127.0.0.1:8000/mcp`; run
`uv run cado info` to inspect the snapshot.

`cado run` starts the server and refreshes the snapshot every 30 days. Refreshes
resume after interruption, and a failed refresh does not replace the current
database.

## MCP

Tools: `search_companies`, `get_company`,
`search_lobbyists`, `get_lobbyist`, and `get_dataset_status`. Searches return
up to 50 records and use `next_offset` for pagination. Each search tool has a
broad `query` for names, affiliated people, and exact registry numbers, plus a
typed `filters` object for classifications, dates, locations, and related
records. Text filters use explicit `any` or `all` matching:

```json
{
  "filters": {
    "director_names": {
      "terms": ["Jack Harrhy", "Martin Whelan"],
      "match": "all"
    },
    "statuses": ["Active"]
  }
}
```

Different filter fields are combined with AND. Search results include
`query_matches` so callers can distinguish a company-name match from, for
example, a current-director match. Registry roles are literal: a director
listing does not by itself establish that someone founded or owns a company.

Public endpoint: `https://cado.jackharrhy.dev/mcp`. This is a periodically
refreshed mirror; check the government registry for legal or time-sensitive
work.

## Docker

Image: `ghcr.io/jackharrhy/cado-search`.

```bash
docker compose pull
docker compose up -d
docker compose logs -f cado
```

The container creates its first snapshot before starting the server, then keeps
serving while later snapshots are built. Set `CADO_REFRESH_INTERVAL_HOURS` to
change the 30-day interval.

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
