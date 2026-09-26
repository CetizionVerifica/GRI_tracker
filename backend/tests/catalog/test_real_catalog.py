"""The real catalog in backend/catalog must always validate and seed cleanly."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.catalog.service import load_catalog
from app.modules.catalog.units import CATALOG_DIR
from tests.catalog.conftest import CATALOG_TABLES, run_seed


def test_real_catalog_validates() -> None:
    files = load_catalog(CATALOG_DIR)

    assert any(f.standard for f in files)


@pytest.fixture
async def clean_catalog(db_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {CATALOG_TABLES} CASCADE"))


@pytest.mark.integration
@pytest.mark.usefixtures("clean_catalog")
async def test_real_catalog_seeds_idempotently(migrated_postgres_url: str) -> None:
    first = await run_seed(migrated_postgres_url, CATALOG_DIR)
    second = await run_seed(migrated_postgres_url, CATALOG_DIR)

    assert first.created["metrics"] > 0
    assert (dict(second.created), dict(second.updated)) == ({}, {})
