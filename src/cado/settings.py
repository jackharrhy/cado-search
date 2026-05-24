"""Runtime configuration. Values can be overridden via environment variables
prefixed with ``CADO_`` (e.g. ``CADO_BASE_URL``), or via a ``.env`` file at the
project root.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    # src/cado/settings.py -> project root is two parents up from src/cado
    return Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Application settings.

    All paths default to ``<project_root>/data/...`` so a fresh checkout works
    out of the box without configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="CADO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "https://cado.eservices.gov.nl.ca"
    user_agent: str = (
        "cado-scraper/0.1 (+https://github.com/jackharrhy/cado; "
        "public-data archival; contact: me@jackharrhy.com)"
    )

    # Rate limiting.
    #
    # Empirically the upstream handles 16 concurrent connections with no
    # backpressure (28/30 successful at conc=16, identical to conc=1), and
    # sustains ~13 req/s effective throughput. We default to those numbers;
    # the global ``requests_per_second`` cap is a soft ceiling that almost
    # never kicks in because the upstream's own ~250-500ms latency dominates.
    requests_per_second: float = 20.0
    max_concurrency: int = 16

    # Connection / timeout knobs (seconds).
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    retries: int = 5

    # Filesystem layout.
    data_dir: Path = Field(default_factory=lambda: _project_root() / "data")

    @property
    def html_cache_dir(self) -> Path:
        return self.data_dir / "html"

    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "cado.duckdb"


settings = Settings()
