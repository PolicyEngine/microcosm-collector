from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from telemetry_collector.database import create_database_engine, normalize_database_url


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """Provide and reset the dedicated PostgreSQL integration-test database."""

    raw_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests.")
    url = normalize_database_url(raw_url)
    if "test" not in (url.database or "").lower():
        pytest.fail("TEST_DATABASE_URL must identify a dedicated test database.")
    engine = create_database_engine(raw_url)

    def reset() -> None:
        metadata = sa.MetaData()
        metadata.reflect(engine)
        metadata.drop_all(engine)

    reset()
    try:
        yield raw_url
    finally:
        reset()
        engine.dispose()
