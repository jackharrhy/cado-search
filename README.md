# cado

Scraper, DuckDB store, and HTMX search UI for the Government of Newfoundland
and Labrador's [Companies and Deeds Online (CADO)](https://cado.eservices.gov.nl.ca/)
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

## Quickstart

```bash
uv sync

# Scrape from upstream (~3 hours for the full company range at defaults)
uv run cado scrape companies --start 1 --stop 105000
uv run cado scrape lobbyists

# Parse the on-disk cache into DuckDB (fast)
uv run cado ingest all

# Serve the search UI on http://127.0.0.1:8000
uv run cado serve
```

`cado info` prints a summary of the on-disk cache and DuckDB row counts.

### On-disk layout

Everything lives under `data_dir` (default: `<project_root>/data/`, override
via `CADO_DATA_DIR`):

```
data/
├── html/                          ← raw scraped HTML cache (source of truth)
│   ├── companies/
│   │   ├── 10/10000.html.gz       ← CompanyDetails.aspx, sharded by first 2 chars
│   │   ├── 2D/2D.html.gz          ← legacy suffixed ids
│   │   └── _list/1.list.html.gz   ← multi-row search results (low numbers only)
│   └── lobbyists/
│       └── IH/IHL-867-1005.html.gz
└── cado.duckdb                    ← derived database (rebuildable from the cache)
```

Approximate sizes once full: ~500 MB for `data/html/companies/`, ~10 MB for
`data/html/lobbyists/`, ~150 MB for `cado.duckdb`.

### Cleaning up

```bash
uv run cado clean db        # drop the DuckDB; rebuild with `cado ingest all`
uv run cado clean cache     # drop the raw HTML cache (destructive! re-scrape needed)
uv run cado clean all       # drop everything
```

All three prompt for confirmation; pass `--yes` to skip.

### Concurrency, rate, and politeness

Defaults: **20 req/s soft cap, 16 concurrent connections**.

These were picked empirically. The upstream:

- handles 16+ concurrent connections cleanly with no observable backpressure
- sustains ~12-14 req/s effective throughput before any diminishing returns
- has ~250-500ms per-request latency, which dominates over any sensible rate cap

For maximum speed, bump concurrency:

```bash
uv run cado scrape companies --concurrency 24   # ~14 req/s, ~2 hr full scrape
uv run cado scrape lobbyists  --concurrency 8   # ~5 min full scrape
```

The scraper sends a descriptive `User-Agent` identifying the project and a
contact email. All settings can be overridden via `--concurrency` / `--rate`
flags or the `CADO_MAX_CONCURRENCY` / `CADO_REQUESTS_PER_SECOND` env vars.

### Resumption

The on-disk HTML cache (`data/html/`) is the source of truth. Every
scrape skips records that are already cached unless `--rescrape` is given,
so interrupted runs resume cleanly. Re-running `cado ingest` is
idempotent: child rows are wiped and reinserted within a single
transaction per record, so you can re-parse with an updated parser
without producing duplicates.

## Layout

```
src/cado/
├── settings.py       # env-driven config (CADO_*)
├── http.py           # CADOClient (httpx) + viewstate + RateLimiter
├── storage.py        # HtmlCache: gzipped HTML on disk, sharded
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
├── api/
│   ├── app.py        # FastAPI factory
│   ├── templates/    # Jinja2 + HTMX
│   └── static/style.css
└── cli.py            # `cado` Typer entry-point
```

## Docker

A multi-arch image (linux/amd64 + linux/arm64) is published to GitHub
Container Registry on every push to `main` and on every `v*.*.*` tag:

```
ghcr.io/jackharrhy/cado:latest
ghcr.io/jackharrhy/cado:sha-<short>
ghcr.io/jackharrhy/cado:v1.2.3      (on tags)
```

The image defaults to running `cado serve --host 0.0.0.0 --port 8000`.
Mount a volume at `/data` to persist the HTML cache and DuckDB across
container restarts:

```bash
docker run -d \
  --name cado \
  -p 8000:8000 \
  -v cado-data:/data \
  ghcr.io/jackharrhy/cado:latest
```

Run scraping and ingest as one-shot commands against the same volume:

```bash
docker run --rm -v cado-data:/data ghcr.io/jackharrhy/cado:latest \
  scrape companies --concurrency 24

docker run --rm -v cado-data:/data ghcr.io/jackharrhy/cado:latest \
  ingest all

docker run --rm -v cado-data:/data ghcr.io/jackharrhy/cado:latest \
  info
```

A `compose.yaml` is included for convenience: `docker compose up`.

## Tests

```bash
uv run pytest                       # 93 offline tests
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
