import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.integration
async def test_real_postgres_is_reachable(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        version: str = (await conn.execute(text("SHOW server_version"))).scalar_one()

    assert str(version).startswith("16")
