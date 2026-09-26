import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory, session_scope
from app.core.errors import ConflictError
from app.modules.tenancy import service
from app.modules.tenancy.schemas import UserCreate
from tests.tenancy.conftest import Seed

pytestmark = pytest.mark.integration

ADMIN = UserCreate(email="ops@platform.test", display_name="Ops", password="bootstrap password 123")


async def _bootstrap(engine: AsyncEngine, data: UserCreate) -> None:
    async for session in session_scope(create_session_factory(engine)):
        await service.bootstrap_platform_admin(session, data)


@pytest.mark.usefixtures("seed")
async def test_bootstrapped_admin_can_log_in_as_platform_admin(
    app_engine: AsyncEngine, api: AsyncClient
) -> None:
    await _bootstrap(app_engine, ADMIN)

    login = await api.post(
        "/api/v1/auth/login", json={"email": ADMIN.email, "password": ADMIN.password}
    )
    me = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )

    assert me.json()["is_platform_admin"] is True


async def test_bootstrap_rejects_existing_email(app_engine: AsyncEngine, seed: Seed) -> None:
    await seed.user("ops@platform.test")

    with pytest.raises(ConflictError):
        await _bootstrap(app_engine, ADMIN)
