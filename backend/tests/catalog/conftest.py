import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.catalog.seed import seed as seed_catalog
from app.modules.catalog.service import SeedReport
from tests.world import Seed, truncate

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG_TABLES = (
    "metric_dimension, metric_definition, dimension_value, dimension, disclosure, standard"
)


async def run_seed(owner_url: str, directory: Path = FIXTURES) -> SeedReport:
    """Seed through the real seed command's entry point, as the owner."""
    return await seed_catalog(directory, owner_url)


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    """A writable copy of the fixture catalog, for tests that edit seed files."""
    target = tmp_path / "catalog"
    shutil.copytree(FIXTURES, target)
    return target


@dataclass
class Catalog:
    standard: UUID
    disclosure_1: UUID
    disclosure_2: UUID
    gross: UUID  # decimal, t, dimension test_gas (required)
    method: UUID  # choice
    test_gas: UUID
    site: UUID
    gas_a: UUID


@pytest.fixture
async def catalog(
    seed: Seed, db_engine: AsyncEngine, migrated_postgres_url: str
) -> AsyncIterator[Catalog]:
    """The fixture catalog, seeded. Depends on seed so it is truncated before tenancy data."""
    await run_seed(migrated_postgres_url)

    async def one(sql: str) -> UUID:
        async with db_engine.connect() as conn:
            value: UUID = (await conn.execute(text(sql))).scalar_one()
        return value

    yield Catalog(
        standard=await one("SELECT id FROM standard WHERE code = 'TEST 900'"),
        disclosure_1=await one("SELECT id FROM disclosure WHERE code = '900-1'"),
        disclosure_2=await one("SELECT id FROM disclosure WHERE code = '900-2'"),
        gross=await one("SELECT id FROM metric_definition WHERE code = 'test900.1.gross'"),
        method=await one("SELECT id FROM metric_definition WHERE code = 'test900.1.method'"),
        test_gas=await one("SELECT id FROM dimension WHERE code = 'test_gas'"),
        site=await one("SELECT id FROM dimension WHERE code = 'site'"),
        gas_a=await one("SELECT id FROM dimension_value WHERE code = 'gas_a'"),
    )
    await truncate(db_engine, CATALOG_TABLES)
