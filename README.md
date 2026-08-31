# cado

Scraper, DuckDB store, HTMX search UI, and MCP server for the Government of
Newfoundland and Labrador's [Companies and Deeds Online (CADO)](https://cado.eservices.gov.nl.ca/)
public registries.

The upstream site is an ASP.NET WebForms app that's workable but
[not very nice to use](https://cado.eservices.gov.nl.ca/). This project
mirrors the publicly available data into a local DuckDB and serves a fast,
ergonomic search UI over it.

## Scope

The four **free** registries:

- Registry of **Companies**
- Registry of **Condominiums**
- Registry of **Co-operatives**
- Registry of **Lobbyists**

Deeds and Mechanics Liens are pay-walled ($5 per search) and are **not**
scraped.

Empirical findings driving the design (all in `tests/fixtures/`):

- Companies / Condos / Co-ops share one numeric id space, discriminated by
  the `lblCorporationType` field. One enumeration covers all three.
- Company id is a **string**, not an integer. Most records are pure
  digits (`25166`) but legacy filings use a digit + uppercase-letter
  suffix scheme (`2D`, `100CM`).
- The active range goes from `1` to roughly `100600` (sweep to `105000`
  for safety).
- An exact-number search 302s straight to `CompanyDetails.aspx` for
  singletons, returns a result list with `_ctlN` postback drill targets
  when multiple records share a digit prefix.
- The lobbyist registry has ~727 records, paginated 10 at a time, with
  the same viewstate-driven postback flow.

## First fetch and local start

```bash
uv sync

# Fetch both registries, rebuild a fresh DuckDB, validate, and publish it.
# The first run takes roughly 2-3 hours and safely resumes if interrupted.
uv run cado refresh

# Serve the search UI and MCP endpoint on http://0.0.0.0:8000
# (pass --host 127.0.0.1 to restrict to localhost)
uv run cado serve
```

The MCP Streamable HTTP endpoint is available from the same process at
`http://127.0.0.1:8000/mcp`. It is read-only and queries the local DuckDB
mirror; tool calls never contact the upstream registry.

## MCP API

The MCP server exposes five structured, read-only tools:

| Tool | Description |
| --- | --- |
| `search_companies` | Search companies, condominiums, and co-operatives by current name or exact number, with type/category/status filters and bounded pagination. |
| `get_company` | Get a complete record including addresses, directors, previous names, historical remarks, and mirror provenance. |
| `search_lobbyists` | Search lobbyist contacts, firms, or exact registration numbers with type/status filters and bounded pagination. |
| `get_lobbyist` | Get a complete mirrored lobbyist registration, including subject matters, targets, techniques, in-house lobbyists, and captured raw fields. |
| `get_dataset_status` | Get per-registry counts, snapshot identity, source-fetch/build/publication timestamps, attribution, and the mirror freshness notice. |

Searches return at most 50 records at a time and include `next_offset` when
another page is available. Every detail result identifies the immutable
snapshot and distinguishes when the source was fetched, the database was
built, and the snapshot was published. Because this is a mirror,
time-sensitive or legal use should be verified against the government registry.

To inspect the HTTP endpoint locally, start `cado serve`, launch the official
MCP Inspector, and connect it to `http://127.0.0.1:8000/mcp`:

```bash
npx -y @modelcontextprotocol/inspector
```

The intended public URL is `https://cado.jackharrhy.dev/mcp`. Override generated
record links and MCP transport allowlists with `CADO_PUBLIC_BASE_URL`,
`CADO_MCP_ALLOWED_HOSTS`, and `CADO_MCP_ALLOWED_ORIGINS`; list-valued settings use
JSON arrays in environment variables.

`cado info` prints a summary of the on-disk cache and DuckDB row counts.

## Snapshot lifecycle

`cado refresh` is the production data path:

1. Acquire an exclusive refresh lock.
2. Fetch into a fresh `data/refresh/` workspace. Gzip files are replaced
   atomically, and company search seeds are journaled only after every drill
   succeeds. Empty seeds are journaled too.
3. Require 1,000 consecutive empty company-number searches at the configured
   high-water mark and require the lobbyist index count to equal its upstream total.
4. Build `cado.next.duckdb` from scratch. Any fetch error, parse error, empty
   registry, count mismatch, schema mismatch, or greater-than-20% count drop
   refuses publication.
5. Checkpoint and close the candidate, retain `cado.previous.duckdb`, then
   atomically replace `cado.duckdb`. The server never opens the live file for writing.
6. Archive the raw snapshot. The newest two raw snapshots and previous database
   are retained for rollback; older raw snapshots are removed.

Re-run the identical command after interruption. If the company high-water
validation fails, increase `--stop`; the existing work is retained. Use
`cado clean refresh --yes` only to intentionally discard an unfinished fetch.

The lower-level `cado scrape` and `cado ingest` commands remain useful for
parser development, but they do not produce a publishable database. `cado
serve` and `cado check` accept only a validated snapshot built by `cado refresh`.

### On-disk layout

Everything lives under `data_dir` (default: `<project_root>/data/`, override
via `CADO_DATA_DIR`):

```
data/
├── cado.duckdb                    ← atomically published, served read-only
├── cado.previous.duckdb           ← immediate rollback copy
├── refresh/                       ← one resumable in-progress snapshot
│   ├── manifest.json
│   └── html/{companies,lobbyists}/
└── snapshots/<snapshot-id>/       ← newest two completed raw snapshots
    ├── manifest.json
    └── html/{companies,lobbyists}/
```

Approximate size per raw snapshot: ~500 MB for companies and ~10 MB for
lobbyists. DuckDB is roughly 150 MB.

### Cleaning up

```bash
uv run cado clean refresh   # discard only an unfinished refresh
uv run cado clean db        # drop the published and previous DuckDB
uv run cado clean cache     # drop staged/archived raw HTML (destructive)
uv run cado clean all       # drop all database and raw snapshot data
```

Destructive commands prompt for confirmation; pass `--yes` to skip.

### Concurrency, rate, and politeness

Defaults: **20 req/s soft cap, 16 concurrent connections**.

These were picked empirically. The upstream:

- handles 16+ concurrent connections cleanly with no observable backpressure
- sustains ~12-14 req/s effective throughput before any diminishing returns
- has ~250-500ms per-request latency, which dominates over any sensible rate cap

For maximum speed, bump concurrency on the complete refresh:

```bash
uv run cado refresh --concurrency 24   # ~14 req/s, ~2 hr full company fetch
```

The scraper sends a descriptive `User-Agent` identifying the project and a
contact email. All settings can be overridden via `--concurrency` / `--rate`
flags or the `CADO_MAX_CONCURRENCY` / `CADO_REQUESTS_PER_SECOND` env vars.

## Layout

```
src/cado/
├── settings.py       # env-driven config (CADO_*)
├── http.py           # CADOClient (httpx) + viewstate + RateLimiter
├── storage.py        # HtmlCache: gzipped HTML on disk, sharded
├── refresh.py        # resumable fetch orchestration
├── snapshot.py       # rebuild, validation, atomic publication, retention
├── models.py         # Pydantic schemas
├── parsers/
│   ├── company.py    # bs4 -> Company / CompanySearchResult
│   └── lobbyist.py   # bs4 -> LobbyistRegistration + pagination helpers
├── scrape/
│   ├── companies.py  # multi-worker enumeration with drill-in
│   └── lobbyists.py  # two-pass index + detail scraper
├── db/
│   ├── schema.sql    # DuckDB DDL
│   ├── session.py    # connect() / init_schema()
│   └── ingest.py     # raw HTML cache -> DuckDB
├── query.py           # shared typed read-only query service
├── mcp.py             # MCP tool definitions
├── api/
│   ├── app.py        # FastAPI factory: HTMX UI + /mcp mount
│   ├── templates/    # Jinja2 + HTMX
│   └── static/style.css
└── cli.py            # `cado` Typer entry-point
```

## Docker: first boot, hosting, and updates

A multi-arch image (linux/amd64 + linux/arm64) is published to GitHub
Container Registry on every push to `main` and on every `v*.*.*` tag:

```
ghcr.io/jackharrhy/cado:latest
ghcr.io/jackharrhy/cado:sha-<short>
ghcr.io/jackharrhy/cado:v1.2.3      (on tags)
```

The public container defaults to serving both the UI and stateless Streamable
HTTP MCP endpoint. Bootstrap the volume before starting it:

```bash
docker compose pull
docker compose run --rm refresh       # first 2-3 hour fetch; safe to rerun
docker compose up -d                  # preflight check gates server startup
docker compose ps
```

The server mounts `/data` read-only. The one-shot refresh container is the
only normal writer. On a fresh volume, the `snapshot-ready` preflight exits
nonzero and Compose does not start the public service until bootstrap succeeds.

Put port 8000 behind the host's TLS reverse proxy and apply request/concurrency
limits there. The intended public endpoints are
`https://cado.jackharrhy.dev/` and `https://cado.jackharrhy.dev/mcp`.

### Periodic updates

Run one full refresh monthly. Scheduling stays outside the web process; the
command has its own non-blocking volume lock, so overlapping refreshes fail:

```cron
0 3 1 * * cd /srv/cado && docker compose run --rm refresh >>/var/log/cado-refresh.log 2>&1
```

The active UI/MCP process continues reading the old immutable database while
the new snapshot is fetched and built. New requests see the replacement after
the atomic promotion; already-open readers finish against the previous file.
Use the same pinned image tag for the running service and scheduled refreshes.
Deploy code/schema changes as a separate maintenance operation, then verify:

```bash
docker compose run --rm snapshot-ready
curl --fail http://127.0.0.1:8000/health/ready
```

Back up the named volume. The raw snapshots are the reparsable source data;
`cado.previous.duckdb` is the immediate database rollback.

## Tests

```bash
uv run pytest                       # offline suite; live tests are skipped
uv run ruff check .
uv run ruff format --check .
uv run mypy src
CADO_LIVE_TESTS=1 uv run pytest     # also runs 7 live tests against the real site
```

Test fixtures under `tests/fixtures/` are captured directly from production
and cover the full diversity of upstream responses: active local companies
with directors, dissolved pre-2004 records with unstructured addresses,
extra-provincial registrations, suffixed legacy ids, condos, co-ops,
multi-row search-result lists, and lobbyist detail pages.

## License

Data is © Government of Newfoundland and Labrador. Code here is
[unlicensed](https://unlicense.org/) — do whatever you want with it.
