# cado

Scraper, DuckDB store, and HTMX search UI for the Government of Newfoundland and
Labrador's [Companies and Deeds Online (CADO)](https://cado.eservices.gov.nl.ca/)
public registries.

The upstream site is an ASP.NET WebForms app that's serviceable but
[not very nice to use](https://cado.eservices.gov.nl.ca/). This project pulls
the publicly available data into a local DuckDB and serves a fast, ergonomic
search UI over it.

## Scope

The four **free** registries are in scope:

- Registry of **Companies**
- Registry of **Condominiums**
- Registry of **Co-operatives**
- Registry of **Lobbyists**

Deeds and Mechanics Liens are pay-walled ($5 per search) and are **not** scraped.

## Status

Work in progress. See [`docs/`](./docs) for design notes and
[`tests/fixtures/`](./tests/fixtures) for captured sample HTML.

## Development

```bash
uv sync
uv run pytest           # offline tests only
CADO_LIVE_TESTS=1 uv run pytest   # also runs a tiny live smoke suite
```

## Politeness

Default settings: **5 requests/sec, max 4 concurrent**. The scraper sends a
descriptive `User-Agent` identifying the project and a contact email.
