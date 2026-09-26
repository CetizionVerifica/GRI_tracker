"""Database rules that hold even if a service check is missing: run as the app role, no service."""

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import RequestContext, bind_request_context, create_session_factory
from app.core.ids import uuid7
from tests.world import Seed, World

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(app_engine: AsyncEngine, seed: Seed) -> AsyncIterator[AsyncSession]:
    """Depends on seed so it rolls back (releasing locks) before seed truncates the tables."""
    async with create_session_factory(app_engine)() as session:
        yield session
        await session.rollback()


async def as_(
    db: AsyncSession, org: UUID | None, user: UUID | None, *, admin: bool = False
) -> None:
    await bind_request_context(db, RequestContext(org, user, admin))


async def test_org_admin_context_cannot_insert_auditor(db: AsyncSession, world: World) -> None:
    await as_(db, world.org_a, world.a_admin)

    with pytest.raises(DBAPIError, match="row-level security"):
        await db.execute(
            text(
                "INSERT INTO role_assignment (id, organization_id, user_id, role, granted_by)"
                " VALUES (:id, :org, :user, 'auditor', :by)"
            ),
            {"id": uuid7(), "org": world.org_a, "user": world.outsider, "by": world.a_admin},
        )


async def test_org_admin_context_cannot_revoke_auditor(db: AsyncSession, world: World) -> None:
    await as_(db, world.org_a, world.a_admin)

    result = await db.execute(
        text(
            "UPDATE role_assignment SET revoked_at = now(), revoked_by = :by"
            " WHERE role = 'auditor' AND organization_id = :org"
        ),
        {"by": world.a_admin, "org": world.org_a},
    )

    assert result.rowcount == 0  # type: ignore[attr-defined]  # the policy hides the row


async def test_tenant_rows_of_other_orgs_are_invisible(db: AsyncSession, world: World) -> None:
    await world.seed.entity(world.org_b, "B1")
    await as_(db, world.org_a, world.a_admin)

    rows = await db.execute(
        text("SELECT count(*) FROM entity WHERE organization_id = :b"), {"b": world.org_b}
    )

    assert rows.scalar_one() == 0


async def test_users_outside_the_org_are_invisible(db: AsyncSession, world: World) -> None:
    await as_(db, world.org_a, world.a_admin)

    emails: set[str] = set((await db.execute(text("SELECT email FROM app_user"))).scalars())

    assert "admin@globex.test" not in emails
    assert "nobody@nowhere.test" not in emails
    assert "auditor@verifier.test" in emails


async def test_app_role_cannot_read_password_hashes(db: AsyncSession, world: World) -> None:
    await as_(db, None, None, admin=True)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text("SELECT password_hash FROM password_credential"))


async def test_cannot_set_someone_elses_password(db: AsyncSession, world: World) -> None:
    await as_(db, world.org_a, world.a_admin)

    with pytest.raises(DBAPIError, match="not allowed to set this password"):
        await db.execute(
            text("SELECT auth_set_password(:user, 'x')"), {"user": world.a_contributor}
        )


@pytest.mark.parametrize("table", ["entity", "reporting_period", "role_assignment", "organization"])
async def test_app_role_cannot_delete(db: AsyncSession, world: World, table: str) -> None:
    await as_(db, world.org_a, world.a_admin, admin=True)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text(f"DELETE FROM {table}"))  # noqa: S608  (fixed test values)


async def test_entity_cannot_change_organization(db: AsyncSession, world: World) -> None:
    await world.seed.entity(world.org_a, "A1")
    await as_(db, world.org_a, world.a_admin)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db.execute(text("UPDATE entity SET organization_id = :b"), {"b": world.org_b})


async def test_published_period_is_immutable_even_for_platform_admin(
    db: AsyncSession, world: World
) -> None:
    period = await world.seed.period(
        world.org_a,
        "FY",
        date(2025, 1, 1),
        date(2025, 12, 31),
        status="published",
        by=world.a_admin,
    )
    await as_(db, world.org_a, world.platform_admin, admin=True)

    with pytest.raises(DBAPIError, match="is published"):
        await db.execute(
            text("UPDATE reporting_period SET name = 'Rewritten' WHERE id = :id"), {"id": period}
        )


async def test_contributor_context_cannot_lock_period(db: AsyncSession, world: World) -> None:
    period = await world.seed.period(world.org_a, "FY", date(2025, 1, 1), date(2025, 12, 31))
    await as_(db, world.org_a, world.a_contributor)

    with pytest.raises(DBAPIError, match="only a platform admin or org admin"):
        await db.execute(
            text(
                "UPDATE reporting_period SET status = 'locked', locked_at = now(), locked_by = :u"
                " WHERE id = :id"
            ),
            {"u": world.a_contributor, "id": period},
        )


async def test_period_cannot_skip_from_open_to_published(db: AsyncSession, world: World) -> None:
    period = await world.seed.period(world.org_a, "FY", date(2025, 1, 1), date(2025, 12, 31))
    await as_(db, world.org_a, world.a_admin)

    with pytest.raises(DBAPIError, match="cannot go from open to published"):
        await db.execute(
            text(
                "UPDATE reporting_period SET status = 'published', locked_at = now(),"
                " locked_by = :u, published_at = now(), published_by = :u WHERE id = :id"
            ),
            {"u": world.a_admin, "id": period},
        )
