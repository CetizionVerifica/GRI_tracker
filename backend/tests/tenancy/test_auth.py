from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.auth import new_session_token
from app.core.ids import uuid7
from tests.world import PASSWORD, Seed, World, problem_code

pytestmark = pytest.mark.integration


# --- login


async def test_login_returns_working_token(api: AsyncClient, world: World) -> None:
    response = await api.post(
        "/api/v1/auth/login", json={"email": "Admin@Acme.test", "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    me = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["id"] == str(world.a_admin)


async def test_login_stores_only_token_digest(api: AsyncClient, world: World, seed: Seed) -> None:
    response = await api.post(
        "/api/v1/auth/login", json={"email": "admin@acme.test", "password": PASSWORD}
    )
    token = response.json()["access_token"]

    async with seed.engine.connect() as conn:
        stored: int = (
            await conn.execute(
                text("SELECT count(*) FROM auth_session WHERE token_hash = :raw"),
                {"raw": token.encode()},
            )
        ).scalar_one()
    assert stored == 0


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("admin@acme.test", "wrong password here"),
        ("unknown@acme.test", PASSWORD),
    ],
)
@pytest.mark.usefixtures("world")
async def test_login_rejects_bad_credentials_the_same_way(
    api: AsyncClient, email: str, password: str
) -> None:
    response = await api.post("/api/v1/auth/login", json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."
    assert response.headers["www-authenticate"] == "Bearer"


async def test_login_rejects_inactive_user(api: AsyncClient, seed: Seed) -> None:
    await seed.user("gone@acme.test", active=False)

    response = await api.post(
        "/api/v1/auth/login", json={"email": "gone@acme.test", "password": PASSWORD}
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [{}, {"email": "not-an-email", "password": "x"}, {"email": "a@b.co", "password": ""}],
)
async def test_login_validates_input(api: AsyncClient, body: dict[str, str]) -> None:
    response = await api.post("/api/v1/auth/login", json=body)

    assert response.status_code == 422


# --- tokens


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic abc"}],
)
async def test_me_requires_valid_token(api: AsyncClient, headers: dict[str, str]) -> None:
    response = await api.get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 401
    assert problem_code(response.json()) == "unauthenticated"


async def test_expired_token_is_rejected(api: AsyncClient, seed: Seed, world: World) -> None:
    token, digest = new_session_token()
    await seed._exec(
        "INSERT INTO auth_session (id, user_id, token_hash, expires_at)"
        " VALUES (:id, :user_id, :digest, :expires)",
        id=uuid7(),
        user_id=world.a_admin,
        digest=digest,
        expires=datetime.now(UTC) - timedelta(seconds=1),
    )

    response = await api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_deactivated_user_token_stops_working(
    api: AsyncClient, seed: Seed, world: World
) -> None:
    await seed._exec("UPDATE app_user SET is_active = false WHERE id = :id", id=world.a_admin)

    response = await api.get("/api/v1/auth/me", headers=world.auth(world.a_admin))

    assert response.status_code == 401


async def test_logout_revokes_token(api: AsyncClient, world: World) -> None:
    headers = world.auth(world.a_admin)

    response = await api.post("/api/v1/auth/logout", headers=headers)

    assert response.status_code == 204
    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_logout_requires_token(api: AsyncClient) -> None:
    assert (await api.post("/api/v1/auth/logout")).status_code == 401


# --- me


async def test_me_lists_active_memberships(api: AsyncClient, world: World, seed: Seed) -> None:
    await seed.grant(world.org_b, world.a_auditor, "auditor")

    response = await api.get("/api/v1/auth/me", headers=world.auth(world.a_auditor))

    assert response.status_code == 200
    body = response.json()
    assert body["is_platform_admin"] is False
    assert {(m["organization_name"], m["role"]) for m in body["memberships"]} == {
        ("Acme", "auditor"),
        ("Globex", "auditor"),
    }


async def test_me_flags_platform_admin(api: AsyncClient, world: World) -> None:
    response = await api.get("/api/v1/auth/me", headers=world.auth(world.platform_admin))

    assert response.json()["is_platform_admin"] is True
    assert response.json()["memberships"] == []


# --- password change


async def test_change_password(api: AsyncClient, world: World) -> None:
    new = "an even longer new password"

    response = await api.put(
        "/api/v1/auth/me/password",
        json={"current_password": PASSWORD, "new_password": new},
        headers=world.auth(world.a_viewer),
    )

    assert response.status_code == 204
    old_login = await api.post(
        "/api/v1/auth/login", json={"email": "viewer@acme.test", "password": PASSWORD}
    )
    new_login = await api.post(
        "/api/v1/auth/login", json={"email": "viewer@acme.test", "password": new}
    )
    assert (old_login.status_code, new_login.status_code) == (401, 200)


async def test_change_password_needs_current_password(api: AsyncClient, world: World) -> None:
    response = await api.put(
        "/api/v1/auth/me/password",
        json={"current_password": "not my password", "new_password": "a brand new password"},
        headers=world.auth(world.a_viewer),
    )

    assert response.status_code == 403


async def test_change_password_rejects_short_password(api: AsyncClient, world: World) -> None:
    response = await api.put(
        "/api/v1/auth/me/password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=world.auth(world.a_viewer),
    )

    assert response.status_code == 422


async def test_change_password_requires_token(api: AsyncClient) -> None:
    response = await api.put(
        "/api/v1/auth/me/password",
        json={"current_password": PASSWORD, "new_password": "a brand new password"},
    )

    assert response.status_code == 401
