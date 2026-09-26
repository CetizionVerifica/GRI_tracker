import pytest
from httpx import AsyncClient

from tests.tenancy.conftest import World

pytestmark = pytest.mark.integration

USERS = "/api/v1/users"
NEW_USER = {
    "email": "  New.Person@Acme.test ",
    "display_name": "New Person",
    "phone": "+919812345678",
    "password": "a sufficiently long password",
}


async def test_platform_admin_creates_user_who_can_log_in(api: AsyncClient, world: World) -> None:
    response = await api.post(USERS, json=NEW_USER, headers=world.auth(world.platform_admin))

    assert response.status_code == 201
    body = response.json()
    assert (body["email"], body["phone"], body["is_active"]) == (
        "new.person@acme.test",
        "+919812345678",
        True,
    )
    assert "password" not in body
    login = await api.post(
        "/api/v1/auth/login",
        json={"email": "new.person@acme.test", "password": NEW_USER["password"]},
    )
    assert login.status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"email": "no-at-sign"},
        {"phone": "0098 1234"},
        {"password": "too short"},
        {"display_name": "   "},
    ],
)
async def test_create_user_validates_input(
    api: AsyncClient, world: World, change: dict[str, str]
) -> None:
    response = await api.post(
        USERS, json=NEW_USER | change, headers=world.auth(world.platform_admin)
    )

    assert response.status_code == 422


async def test_create_user_rejects_duplicate_email_case_insensitively(
    api: AsyncClient, world: World
) -> None:
    response = await api.post(
        USERS,
        json=NEW_USER | {"email": "ADMIN@acme.test"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 409


async def test_org_admin_cannot_create_user(api: AsyncClient, world: World) -> None:
    response = await api.post(USERS, json=NEW_USER, headers=world.auth(world.a_admin))

    assert response.status_code == 403


async def test_create_user_requires_token(api: AsyncClient) -> None:
    assert (await api.post(USERS, json=NEW_USER)).status_code == 401
