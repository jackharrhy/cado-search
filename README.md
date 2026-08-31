# cado

Cado copies Newfoundland and Labrador's free [Companies and Deeds Online
(CADO)](https://cado.eservices.gov.nl.ca/) registries into DuckDB and gives them
a simpler search page. It covers companies, condominiums, co-operatives, and
lobbyists. Deeds and Mechanics Liens are left out because they cost $5 per
search.

The server also has a read-only, stateless MCP endpoint.

## Run it locally

```bash
uv sync

# Fetch both registries, rebuild a fresh DuckDB, validate, and publish it.
# The first run takes roughly 2-3 hours and safely resumes if interrupted.
uv run cado refresh

# Serve the search UI and MCP endpoint on http://0.0.0.0:8000
# (pass --host 127.0.0.1 to restrict to localhost)
uv run cado serve
```

Open `http://127.0.0.1:8000` for the site or
`http://127.0.0.1:8000/mcp` for MCP. `cado info` shows what is in the local
snapshot.

`cado refresh` can be run again after an interruption. It keeps its downloaded
work, builds and checks a new database, then swaps it into place. The server
only reads the published database. A failed refresh leaves the old one alone.

## MCP

The Streamable HTTP endpoint has five tools:

| Tool | What it does |
| --- | --- |
| `search_companies` | Search companies, condominiums, and co-operatives. |
| `get_company` | Get a company record, including addresses, directors, old names, and remarks. |
| `search_lobbyists` | Search lobbyist contacts, firms, and registration numbers. |
| `get_lobbyist` | Get a full lobbyist registration. |
| `get_dataset_status` | Show snapshot dates and record counts. |

Searches return up to 50 records at a time and include `next_offset` when there
are more. Results come from a mirror, so check the government registry for
legal or time-sensitive work.

To inspect the endpoint:

```bash
npx -y @modelcontextprotocol/inspector
```

Connect the inspector to `http://127.0.0.1:8000/mcp`. The public endpoint is
`https://cado.jackharrhy.dev/mcp`.

## Docker

Images are published at `ghcr.io/jackharrhy/cado-search`. The `latest` tag is
built from `main` for both amd64 and arm64.

The first start needs a snapshot:

```bash
docker compose pull
docker compose run --rm refresh       # first 2-3 hour fetch; safe to rerun
docker compose up -d
docker compose ps
```

Both services use the same image and data volume. `cado` is the server and can
only read the volume; `refresh` is a one-shot job that can write it. The server
checks the snapshot when it starts and exits if it is missing or invalid.

For public use, put port 8000 behind a TLS proxy. Set
`CADO_PUBLIC_BASE_URL` for generated links. The MCP host and origin allowlists
use `CADO_MCP_ALLOWED_HOSTS` and `CADO_MCP_ALLOWED_ORIGINS`, both as JSON arrays.

## Updating the snapshot

Run a full refresh about once a month:

```cron
0 3 1 * * cd /srv/cado && docker compose run --rm refresh >>/var/log/cado-refresh.log 2>&1
```

The current database stays online while the new one is fetched and built.
Refreshes use a lock, so two cannot write at once. Keep the server and refresh
job on the same image tag.

After an update:

```bash
docker compose run --rm cado check
curl --fail http://127.0.0.1:8000/health/ready
```

Back up the `/data` volume. It contains the current database, the previous
database, and the two newest raw snapshots.

## Development

```bash
uv run pytest                       # offline suite; live tests are skipped
uv run ruff check .
uv run ruff format --check .
uv run mypy src
CADO_LIVE_TESTS=1 uv run pytest     # also runs 7 live tests against the real site
```

The fixtures in `tests/fixtures/` are pages captured from the production site.
Use the lower-level `cado scrape` and `cado ingest` commands when working on
parsers. They do not create a snapshot that `cado serve` will accept.

## License

The data is © Government of Newfoundland and Labrador. The code is
[unlicensed](https://unlicense.org/). Do whatever you want with it.
