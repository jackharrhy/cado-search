"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# Network-touching tests are opt-in via CADO_LIVE_TESTS=1 so CI / casual `pytest`
# runs never hit the public site.
live_required = pytest.mark.skipif(
    os.environ.get("CADO_LIVE_TESTS") != "1",
    reason="set CADO_LIVE_TESTS=1 to run tests that hit cado.eservices.gov.nl.ca",
)
