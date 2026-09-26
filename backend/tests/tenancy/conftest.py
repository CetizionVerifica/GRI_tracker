"""Tenancy test world: two organizations and users with different roles, seeded as the owner."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.auth import hash_password, new_session_token
from app.core.config import Settings
from app.core.ids import uuid7
from app.main import create_app
from tests.conftest import make_client

PASSWORD = "correct horse battery staple"
TENANCY_TABLES = (
    "role_assignment, platform_role_assignment, auth_session, password_credential,"
    " reporting_period, entity, app_user, organization"
)


@cache
def password_hash() -> str:
    return hash_password(PASSWORD)


Headers = dict[str, str]


@dataclass
class Seed:
    """Writes fixtures straight into the database as the owner, bypassing row-level security."""

    engine: AsyncEngine
    headers: dict[UUID, Headers] = field(default_factory=dict)

    async def _exec(self, sql: str, **params: object) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    async def user(self, email: str, *, platform_admin: bool = False, active: bool = True) -> UUID:
        user_id = uuid7()
        await self._exec(
            "INSERT INTO app_user (id, email, display_name, is_active)"
            " VALUES (:id, :email, :name, :active)",
            id=user_id,
            email=email,
            name=email.split("@")[0],
            active=active,
        )
        await self._exec(
            "INSERT INTO password_credential (user_id, password_hash) VALUES (:id, :hash)",
            id=user_id,
            hash=password_hash(),
        )
        if platform_admin:
            await self._exec(
                "INSERT INTO platform_role_assignment (id, user_id, role)"
                " VALUES (:id, :user_id, 'platform_admin')",
                id=uuid7(),
                user_id=user_id,
            )
        token, digest = new_session_token()
        await self._exec(
            "INSERT INTO auth_session (id, user_id, token_hash, expires_at)"
            " VALUES (:id, :user_id, :digest, :expires)",
            id=uuid7(),
            user_id=user_id,
            digest=digest,
            expires=datetime.now(UTC) + timedelta(hours=1),
        )
        self.headers[user_id] = {"Authorization": f"Bearer {token}"}
        return user_id

    async def org(self, slug: str, *, status: str = "active") -> UUID:
        org_id = uuid7()
        await self._exec(
            "INSERT INTO organization (id, name, slug, status) VALUES (:id, :name, :slug, :status)",
            id=org_id,
            name=slug.title(),
            slug=slug,
            status=status,
        )
        return org_id

    async def grant(
        self, org_id: UUID, user_id: UUID, role: str, *, entity_id: UUID | None = None
    ) -> UUID:
        assignment_id = uuid7()
        await self._exec(
            "INSERT INTO role_assignment"
            " (id, organization_id, user_id, role, entity_id, granted_by)"
            " VALUES (:id, :org, :user_id, :role, :entity, :user_id)",
            id=assignment_id,
            org=org_id,
            user_id=user_id,
            role=role,
            entity=entity_id,
        )
        return assignment_id

    async def entity(
        self,
        org_id: UUID,
        code: str,
        *,
        parent_id: UUID | None = None,
        archived: bool = False,
        kind: str = "site",
    ) -> UUID:
        entity_id = uuid7()
        await self._exec(
            "INSERT INTO entity (id, organization_id, parent_id, code, name, kind, archived_at)"
            " VALUES (:id, :org, :parent, :code, :code, :kind, :archived)",
            id=entity_id,
            org=org_id,
            parent=parent_id,
            code=code,
            kind=kind,
            archived=datetime.now(UTC) if archived else None,
        )
        return entity_id

    async def period(
        self,
        org_id: UUID,
        name: str,
        start: date,
        end: date,
        *,
        status: str = "open",
        by: UUID | None = None,
    ) -> UUID:
        period_id = uuid7()
        now = datetime.now(UTC)
        locked = status in ("locked", "published")
        published = status == "published"
        await self._exec(
            "INSERT INTO reporting_period (id, organization_id, name, start_date, end_date)"
            " VALUES (:id, :org, :name, :start, :end)",
            id=period_id,
            org=org_id,
            name=name,
            start=start,
            end=end,
        )
        if locked:
            # The guard trigger also applies to the owner, so set the admin context for it.
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT set_config('app.is_platform_admin', 'true', true)"))
                await conn.execute(
                    text(
                        "UPDATE reporting_period SET status = 'locked', locked_at = :now,"
                        " locked_by = :by WHERE id = :id"
                    ),
                    {"now": now, "by": by, "id": period_id},
                )
                if published:
                    await conn.execute(
                        text(
                            "UPDATE reporting_period SET status = 'published',"
                            " published_at = :now, published_by = :by WHERE id = :id"
                        ),
                        {"now": now, "by": by, "id": period_id},
                    )
        return period_id


@dataclass
class World:
    seed: Seed
    org_a: UUID
    org_b: UUID
    platform_admin: UUID
    a_admin: UUID
    a_contributor: UUID
    a_viewer: UUID
    a_auditor: UUID
    b_admin: UUID
    outsider: UUID  # active user with no roles anywhere

    def auth(self, user_id: UUID) -> Headers:
        return self.seed.headers[user_id]


@pytest.fixture
async def seed(migrated_postgres_url: str, db_engine: AsyncEngine) -> AsyncIterator[Seed]:
    yield Seed(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TENANCY_TABLES} CASCADE"))


@pytest.fixture
async def world(seed: Seed) -> World:
    org_a = await seed.org("acme")
    org_b = await seed.org("globex")
    platform_admin = await seed.user("root@platform.test", platform_admin=True)
    a_admin = await seed.user("admin@acme.test")
    a_contributor = await seed.user("contrib@acme.test")
    a_viewer = await seed.user("viewer@acme.test")
    a_auditor = await seed.user("auditor@verifier.test")
    b_admin = await seed.user("admin@globex.test")
    outsider = await seed.user("nobody@nowhere.test")
    await seed.grant(org_a, a_admin, "org_admin")
    await seed.grant(org_a, a_contributor, "contributor")
    await seed.grant(org_a, a_viewer, "viewer")
    await seed.grant(org_a, a_auditor, "auditor")
    await seed.grant(org_b, b_admin, "org_admin")
    return World(
        seed,
        org_a,
        org_b,
        platform_admin,
        a_admin,
        a_contributor,
        a_viewer,
        a_auditor,
        b_admin,
        outsider,
    )


@pytest.fixture
def tenancy_settings(settings: Settings, app_database_url: str) -> Settings:
    return settings.model_copy(update={"database_url": app_database_url})


@pytest.fixture
async def api(tenancy_settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(tenancy_settings)
    async for client in make_client(app):
        yield client


def problem_code(response_json: object) -> str:
    assert isinstance(response_json, dict)
    problem_type = response_json["type"]
    assert isinstance(problem_type, str)
    return problem_type.removeprefix("urn:gri-kpi:problem:")
