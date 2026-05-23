"""CADO: scraper, DuckDB store, and HTMX search UI for Newfoundland's CADO registries.

The site at https://cado.eservices.gov.nl.ca/ ("Companies and Deeds Online") is a
classic ASP.NET WebForms application served by the Government of Newfoundland and
Labrador. This package fetches publicly available registry data, stores raw HTML
on disk, parses it into a normalised DuckDB database, and exposes a search UI.
"""

__version__ = "0.1.0"
