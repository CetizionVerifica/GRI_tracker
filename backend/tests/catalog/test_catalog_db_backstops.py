"""Catalog rules that hold in the database even without the service: run as the app role."""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import RequestContext, bind_request_context, create_session_factory
from app.core.ids import uuid7
from tests.catalog.conftest import Catalog
from tests.world import World

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(app_engine: AsyncEngine, catalog: Catalog) -> AsyncIterator[AsyncSession]:
    """Depends on catalog so it rolls back (releasing locks) before the tables are truncated."""
    async with create_session_factory(app_engine)() as session:
        yield session
        await session.rollback()


async def in_org(db: AsyncSession, org: UUID, *, admin: bool = False) -> None:
    await bind_request_context(db, RequestContext(org, None, admin))


async def test_app_cannot_write_global_rows(db: AsyncSession, world: World) -> None:
    await in_org(db, world.org_a, admin=True)

    with pytest.raises(DBAPIError, match="row-level security"):
        await db.execute(
            text("INSERT INTO dimension (id, code, name) VALUES (:id, 'hacked', 'Hacked')"),
            {"id": uuid7()},
        )


async def test_app_cannot_change_global_rows(
    db: AsyncSession, catalog: Catalog, world: World
) -> None:
    await in_org(db, world.org_a, admin=True)

    result = await db.execute(
        text("UPDATE dimension_value SET label = 'Hacked' WHERE id = :id"), {"id": catalog.gas_a}
    )

    assert result.rowcount == 0  # type: ignore[attr-defined]  # the policy hides global rows


@pytest.mark.parametrize("table", ["standard", "disclosure"])
async def test_app_cannot_write_standards(db: AsyncSession, world: World, table: str) -> None:
    await in_org(db, world.org_a, admin=True)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text(f"UPDATE {table} SET code = code"))  # noqa: S608


@pytest.mark.parametrize("column", ["data_type", "unit", "validation_rules", "code"])
async def test_metric_structure_is_never_updatable(
    db: AsyncSession, world: World, column: str
) -> None:
    await in_org(db, world.org_a)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text(f"UPDATE metric_definition SET {column} = {column}"))  # noqa: S608


async def test_tenant_value_on_other_tenants_dimension_is_blocked(
    db: AsyncSession, world: World, db_engine: AsyncEngine
) -> None:
    foreign = uuid7()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dimension (id, organization_id, code, name)"
                " VALUES (:id, :org, 'custom.plant', 'Plant')"
            ),
            {"id": foreign, "org": world.org_b},
        )
    await in_org(db, world.org_a)

    with pytest.raises(DBAPIError, match="belongs to another organization"):
        await db.execute(
            text(
                "INSERT INTO dimension_value (id, organization_id, dimension_id, code, label)"
                " VALUES (:id, :org, :dim, 'custom.x', 'X')"
            ),
            {"id": uuid7(), "org": world.org_a, "dim": foreign},
        )


async def test_tenant_cannot_add_dimensions_to_global_metrics(
    db: AsyncSession, world: World, catalog: Catalog
) -> None:
    await in_org(db, world.org_a)

    with pytest.raises(DBAPIError, match="same scope as its metric"):
        await db.execute(
            text(
                "INSERT INTO metric_dimension (metric_definition_id, dimension_id, organization_id)"
                " VALUES (:metric, :dim, :org)"
            ),
            {"metric": catalog.gross, "dim": catalog.site, "org": world.org_a},
        )


async def test_app_cannot_delete_catalog_rows(db: AsyncSession, world: World) -> None:
    await in_org(db, world.org_a)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text("DELETE FROM metric_definition"))
