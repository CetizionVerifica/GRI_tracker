from uuid import UUID

import pytest
from httpx import AsyncClient

from app.core.ids import uuid7
from tests.tenancy.conftest import World

pytestmark = pytest.mark.integration


def url(org: UUID, assignment: UUID | None = None) -> str:
    base = f"/api/v1/organizations/{org}/role-assignments"
    return f"{base}/{assignment}/revoke" if assignment else base


# --- grant


async def test_org_admin_grants_role_by_email(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_a),
        json={"email": "Nobody@Nowhere.test", "role": "approver"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["user_id"], body["role"], body["granted_by"]) == (
        str(world.outsider),
        "approver",
        str(world.a_admin),
    )
    orgs = await api.get("/api/v1/organizations", headers=world.auth(world.outsider))
    assert [o["slug"] for o in orgs.json()] == ["acme"]


async def test_entity_scoped_role(api: AsyncClient, world: World) -> None:
    site = await world.seed.entity(world.org_a, "PUNE")

    response = await api.post(
        url(world.org_a),
        json={"email": "nobody@nowhere.test", "role": "contributor", "entity_id": str(site)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201
    assert response.json()["entity_id"] == str(site)


@pytest.mark.parametrize(
    "body",
    [
        {"email": "nobody@nowhere.test", "role": "superuser"},
        {"email": "not-an-email", "role": "viewer"},
        {"email": "nobody@nowhere.test", "role": "org_admin", "entity_id": str(uuid7())},
        {"email": "nobody@nowhere.test", "role": "auditor", "entity_id": str(uuid7())},
    ],
)
async def test_grant_validates_input(api: AsyncClient, world: World, body: dict[str, str]) -> None:
    response = await api.post(url(world.org_a), json=body, headers=world.auth(world.a_admin))

    assert response.status_code == 422


async def test_grant_to_unknown_email_is_404(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_a),
        json={"email": "ghost@nowhere.test", "role": "viewer"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_grant_duplicate_role_conflicts(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_a),
        json={"email": "viewer@acme.test", "role": "viewer"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


async def test_grant_scoped_to_other_tenants_entity_is_404(api: AsyncClient, world: World) -> None:
    foreign = await world.seed.entity(world.org_b, "B1")

    response = await api.post(
        url(world.org_a),
        json={"email": "nobody@nowhere.test", "role": "viewer", "entity_id": str(foreign)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_org_admin_cannot_grant_auditor(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_a),
        json={"email": "nobody@nowhere.test", "role": "auditor"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 403


async def test_platform_admin_grants_auditor(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_b),
        json={"email": "auditor@verifier.test", "role": "auditor"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 201


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_non_admin_cannot_grant(api: AsyncClient, world: World, role: str) -> None:
    response = await api.post(
        url(world.org_a),
        json={"email": "nobody@nowhere.test", "role": "viewer"},
        headers=world.auth(getattr(world, role)),
    )

    assert response.status_code == 403


async def test_grant_in_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_b),
        json={"email": "admin@acme.test", "role": "org_admin"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_grant_requires_token(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_a), json={"email": "nobody@nowhere.test", "role": "viewer"}
    )

    assert response.status_code == 401


# --- list


@pytest.mark.parametrize("role", ["a_admin", "a_auditor", "platform_admin"])
async def test_admins_and_auditors_list_assignments(
    api: AsyncClient, world: World, role: str
) -> None:
    response = await api.get(url(world.org_a), headers=world.auth(getattr(world, role)))

    assert response.status_code == 200
    assert {(a["user_email"], a["role"]) for a in response.json()} == {
        ("admin@acme.test", "org_admin"),
        ("contrib@acme.test", "contributor"),
        ("viewer@acme.test", "viewer"),
        ("auditor@verifier.test", "auditor"),
    }


async def test_list_can_include_revoked(api: AsyncClient, world: World) -> None:
    await world.seed._exec(
        "UPDATE role_assignment SET revoked_at = now(), revoked_by = :by WHERE user_id = :u",
        by=world.a_admin,
        u=world.a_viewer,
    )
    headers = world.auth(world.a_auditor)

    active = await api.get(url(world.org_a), headers=headers)
    history = await api.get(url(world.org_a), params={"include_revoked": True}, headers=headers)

    assert len(active.json()) == 3
    assert len(history.json()) == 4


async def test_list_bad_query_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(
        url(world.org_a), params={"include_revoked": "maybe"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer"])
async def test_others_cannot_list_assignments(api: AsyncClient, world: World, role: str) -> None:
    response = await api.get(url(world.org_a), headers=world.auth(getattr(world, role)))

    assert response.status_code == 403


async def test_list_in_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    assert (await api.get(url(world.org_b), headers=world.auth(world.a_admin))).status_code == 404


async def test_list_requires_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(url(world.org_a))).status_code == 401


# --- revoke


async def test_org_admin_revokes_role(api: AsyncClient, world: World) -> None:
    assignment = await world.seed.grant(world.org_a, world.outsider, "viewer")

    response = await api.post(url(world.org_a, assignment), headers=world.auth(world.a_admin))

    assert response.status_code == 200
    assert response.json()["revoked_by"] == str(world.a_admin)
    orgs = await api.get(f"/api/v1/organizations/{world.org_a}", headers=world.auth(world.outsider))
    assert orgs.status_code == 404


async def test_revoke_twice_conflicts(api: AsyncClient, world: World) -> None:
    assignment = await world.seed.grant(world.org_a, world.outsider, "viewer")
    headers = world.auth(world.a_admin)

    await api.post(url(world.org_a, assignment), headers=headers)
    again = await api.post(url(world.org_a, assignment), headers=headers)

    assert again.status_code == 409


async def test_revoke_bad_id_is_422(api: AsyncClient, world: World) -> None:
    response = await api.post(f"{url(world.org_a)}/nope/revoke", headers=world.auth(world.a_admin))

    assert response.status_code == 422


async def test_org_admin_cannot_revoke_auditor(api: AsyncClient, world: World) -> None:
    auditor_grant = await world.seed.grant(world.org_a, world.outsider, "auditor")

    by_org_admin = await api.post(
        url(world.org_a, auditor_grant), headers=world.auth(world.a_admin)
    )
    by_platform_admin = await api.post(
        url(world.org_a, auditor_grant), headers=world.auth(world.platform_admin)
    )

    assert (by_org_admin.status_code, by_platform_admin.status_code) == (403, 200)


async def test_contributor_cannot_revoke(api: AsyncClient, world: World) -> None:
    assignment = await world.seed.grant(world.org_a, world.outsider, "viewer")

    response = await api.post(url(world.org_a, assignment), headers=world.auth(world.a_contributor))

    assert response.status_code == 403


async def test_revoke_in_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await world.seed.grant(world.org_b, world.outsider, "viewer")

    via_own_org = await api.post(url(world.org_a, foreign), headers=world.auth(world.a_admin))
    via_their_org = await api.post(url(world.org_b, foreign), headers=world.auth(world.a_admin))

    assert (via_own_org.status_code, via_their_org.status_code) == (404, 404)


async def test_revoke_requires_token(api: AsyncClient, world: World) -> None:
    assignment = await world.seed.grant(world.org_a, world.outsider, "viewer")

    assert (await api.post(url(world.org_a, assignment))).status_code == 401
