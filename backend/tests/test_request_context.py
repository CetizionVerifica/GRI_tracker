"""Row-level security plumbing: the app role, the context functions and bind_request_context."""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import RequestContext, bind_request_context, create_session_factory
from app.core.ids import uuid7

pytestmark = pytest.mark.integration

ORG_A = uuid7()
ORG_B = uuid7()


@pytest.fixture
async def probe_table(db_engine: AsyncEngine) -> AsyncIterator[None]:
    """A tenant table with the same policy shape the real tenant tables will use."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE rls_probe (id uuid PRIMARY KEY, organization_id uuid NOT NULL)")
        )
        await conn.execute(text("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                "CREATE POLICY tenant_isolation ON rls_probe"
                " USING (organization_id = app_current_org_id())"
                " WITH CHECK (organization_id = app_current_org_id())"
            )
        )
        await conn.execute(text("GRANT SELECT, INSERT ON rls_probe TO gri_app"))
        await conn.execute(
            text("INSERT INTO rls_probe VALUES (:a1, :org_a), (:a2, :org_a), (:b1, :org_b)"),
            {"a1": uuid7(), "a2": uuid7(), "b1": uuid7(), "org_a": ORG_A, "org_b": ORG_B},
        )
    yield
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE rls_probe"))


@pytest.fixture
async def app_session(app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with create_session_factory(app_engine)() as session:
        yield session


async def _visible_orgs(session: AsyncSession) -> list[UUID]:
    result = await session.execute(text("SELECT organization_id FROM rls_probe"))
    return list(result.scalars())


async def test_app_role_cannot_bypass_rls(app_session: AsyncSession) -> None:
    row = (
        await app_session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).one()

    assert tuple(row) == (False, False)


@pytest.mark.usefixtures("probe_table")
async def test_no_context_sees_nothing(app_session: AsyncSession) -> None:
    assert await _visible_orgs(app_session) == []


@pytest.mark.usefixtures("probe_table")
async def test_context_limits_rows_to_one_organization(app_session: AsyncSession) -> None:
    await bind_request_context(app_session, RequestContext(organization_id=ORG_A, user_id=None))

    assert await _visible_orgs(app_session) == [ORG_A, ORG_A]


@pytest.mark.usefixtures("probe_table")
async def test_context_survives_commit(app_session: AsyncSession) -> None:
    await bind_request_context(app_session, RequestContext(organization_id=ORG_B, user_id=None))
    await _visible_orgs(app_session)
    await app_session.commit()

    assert await _visible_orgs(app_session) == [ORG_B]


@pytest.mark.usefixtures("probe_table")
async def test_binding_inside_open_transaction_applies_immediately(
    app_session: AsyncSession,
) -> None:
    assert await _visible_orgs(app_session) == []  # opens the transaction

    await bind_request_context(app_session, RequestContext(organization_id=ORG_B, user_id=None))

    assert await _visible_orgs(app_session) == [ORG_B]


@pytest.mark.usefixtures("probe_table")
async def test_context_does_not_leak_to_next_session(app_engine: AsyncEngine) -> None:
    factory = create_session_factory(app_engine)
    async with factory() as first:
        await bind_request_context(first, RequestContext(organization_id=ORG_A, user_id=None))
        assert await _visible_orgs(first) == [ORG_A, ORG_A]
        await first.commit()

    async with factory() as second:
        assert await _visible_orgs(second) == []


@pytest.mark.usefixtures("probe_table")
async def test_cannot_write_into_another_organization(app_session: AsyncSession) -> None:
    await bind_request_context(app_session, RequestContext(organization_id=ORG_A, user_id=None))

    with pytest.raises(ProgrammingError, match="row-level security"):
        await app_session.execute(
            text("INSERT INTO rls_probe VALUES (:id, :org)"), {"id": uuid7(), "org": ORG_B}
        )


async def test_context_functions_read_the_bound_values(app_session: AsyncSession) -> None:
    user = uuid7()
    await bind_request_context(
        app_session, RequestContext(organization_id=ORG_A, user_id=user, is_platform_admin=True)
    )

    row = (
        await app_session.execute(
            text("SELECT app_current_org_id(), app_current_user_id(), app_is_platform_admin()")
        )
    ).one()

    assert tuple(row) == (ORG_A, user, True)


async def test_context_functions_default_to_nothing(app_session: AsyncSession) -> None:
    row = (
        await app_session.execute(
            text("SELECT app_current_org_id(), app_current_user_id(), app_is_platform_admin()")
        )
    ).one()

    assert tuple(row) == (None, None, False)
