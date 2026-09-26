"""Database guarantees of the audit log: append-only, hash-chained, tenant-isolated, unforgeable."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import RequestContext, bind_request_context, create_session_factory
from app.core.ids import uuid7
from app.modules.audit import service as audit
from tests.world import Seed, World

pytestmark = pytest.mark.integration


@pytest.fixture
async def app_db(app_engine: AsyncEngine, seed: Seed) -> AsyncIterator[AsyncSession]:
    """App-role session; depends on seed so it rolls back before the tables are emptied."""
    async with create_session_factory(app_engine)() as session:
        yield session
        await session.rollback()


async def record_as(
    engine: AsyncEngine, org: UUID | None, user: UUID, action: str, *, admin: bool = False
) -> None:
    async with create_session_factory(engine)() as session:
        await bind_request_context(session, RequestContext(org, user, admin))
        await audit.record(
            session,
            organization_id=org,
            actor_user_id=user,
            action=action,
            target_table="entity",
            target_id=uuid7(),
        )
        await session.commit()


async def chain(engine: AsyncEngine, org: UUID | None) -> list[tuple[int, bytes, bytes]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT seq, prev_hash, row_hash FROM audit_log"
                " WHERE organization_id IS NOT DISTINCT FROM :org ORDER BY seq"
            ),
            {"org": org},
        )
        return [(r.seq, r.prev_hash, r.row_hash) for r in rows]


async def verify(engine: AsyncEngine, org: UUID | None) -> tuple[int, int | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT * FROM audit_verify_chain(:org)"), {"org": org})
        ).one()
        return row.rows_checked, row.first_broken_seq


async def test_entries_form_a_linked_chain(
    app_engine: AsyncEngine, db_engine: AsyncEngine, world: World
) -> None:
    for action in ("entity.created", "entity.updated", "entity.updated"):
        await record_as(app_engine, world.org_a, world.a_admin, action)

    rows = await chain(db_engine, world.org_a)

    assert [seq for seq, _, _ in rows] == [1, 2, 3]
    assert rows[0][1] == bytes(32)  # genesis
    assert rows[1][1] == rows[0][2]
    assert rows[2][1] == rows[1][2]
    assert await verify(db_engine, world.org_a) == (3, None)


async def test_each_organization_has_its_own_chain(
    app_engine: AsyncEngine, db_engine: AsyncEngine, world: World
) -> None:
    await record_as(app_engine, world.org_a, world.a_admin, "entity.created")
    await record_as(app_engine, world.org_b, world.b_admin, "entity.created")

    assert [r[0] for r in await chain(db_engine, world.org_a)] == [1]
    assert [r[0] for r in await chain(db_engine, world.org_b)] == [1]


async def test_concurrent_inserts_keep_the_chain_intact(
    app_engine: AsyncEngine, db_engine: AsyncEngine, world: World
) -> None:
    await asyncio.gather(
        *(record_as(app_engine, world.org_a, world.a_admin, f"test.{i}") for i in range(20))
    )

    assert [r[0] for r in await chain(db_engine, world.org_a)] == list(range(1, 21))
    assert await verify(db_engine, world.org_a) == (20, None)


async def test_tampering_is_detected(
    app_engine: AsyncEngine, db_engine: AsyncEngine, world: World
) -> None:
    for action in ("a.one", "a.two", "a.three"):
        await record_as(app_engine, world.org_a, world.a_admin, action)
    async with db_engine.begin() as conn:  # even the owner must switch off the guard first
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete"))
        await conn.execute(
            text("UPDATE audit_log SET action = 'a.forged' WHERE organization_id = :o AND seq = 2"),
            {"o": world.org_a},
        )
        await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete"))

    assert await verify(db_engine, world.org_a) == (2, 2)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
async def test_owner_cannot_change_entries(
    app_engine: AsyncEngine, db_engine: AsyncEngine, world: World, statement: str
) -> None:
    await record_as(app_engine, world.org_a, world.a_admin, "entity.created")

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"]
)
async def test_app_role_has_no_update_or_delete(
    app_db: AsyncSession, world: World, statement: str
) -> None:
    await bind_request_context(app_db, RequestContext(world.org_a, world.a_admin, True))

    with pytest.raises(DBAPIError, match="permission denied"):
        await app_db.execute(text(statement))


async def test_database_assigns_seq_time_and_hashes(
    app_db: AsyncSession, db_engine: AsyncEngine, world: World
) -> None:
    await bind_request_context(app_db, RequestContext(world.org_a, world.a_admin))
    await app_db.execute(
        text(
            "INSERT INTO audit_log (id, organization_id, seq, occurred_at, actor_user_id,"
            " actor_type, action, target_table, prev_hash, row_hash) VALUES (:id, :org, 999,"
            " '2000-01-01', :user, 'user', 'x.forged', 'entity', '\\x00', '\\x00')"
        ),
        {"id": uuid7(), "org": world.org_a, "user": world.a_admin},
    )
    await app_db.commit()

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT seq, occurred_at > now() - interval '1 minute' AS fresh,"
                    " length(row_hash) AS n FROM audit_log WHERE organization_id = :o"
                ),
                {"o": world.org_a},
            )
        ).one()
    assert (row.seq, row.fresh, row.n) == (1, True, 32)


@pytest.mark.parametrize(
    ("org_attr", "actor_attr"),
    [
        ("org_b", "a_admin"),  # another organization's chain
        ("org_a", "a_viewer"),  # pretending to be someone else
    ],
)
async def test_entries_cannot_be_forged(
    app_db: AsyncSession, world: World, org_attr: str, actor_attr: str
) -> None:
    await bind_request_context(app_db, RequestContext(world.org_a, world.a_admin))

    with pytest.raises(DBAPIError, match="row-level security"):
        await audit.record(
            app_db,
            organization_id=getattr(world, org_attr),
            actor_user_id=getattr(world, actor_attr),
            action="x.forged",
            target_table="entity",
            target_id=None,
        )


async def test_system_entries_need_a_platform_admin(app_db: AsyncSession, world: World) -> None:
    await bind_request_context(app_db, RequestContext(world.org_a, world.a_admin))

    with pytest.raises(DBAPIError, match="row-level security"):
        await audit.record(
            app_db,
            organization_id=world.org_a,
            actor_user_id=None,
            action="x.system",
            target_table="entity",
            target_id=None,
        )


async def test_reads_are_tenant_isolated(
    app_engine: AsyncEngine, app_db: AsyncSession, world: World
) -> None:
    await record_as(app_engine, world.org_b, world.b_admin, "entity.created")
    await record_as(app_engine, None, world.platform_admin, "platform.event", admin=True)
    await bind_request_context(app_db, RequestContext(world.org_a, world.a_admin))

    visible: int = (await app_db.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()

    assert visible == 0
