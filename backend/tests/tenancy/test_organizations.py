import pytest
from httpx import AsyncClient

from tests.tenancy.conftest import Seed, World, problem_code

pytestmark = pytest.mark.integration

ORGS = "/api/v1/organizations"


# --- create (platform admin only)


async def test_platform_admin_creates_organization(api: AsyncClient, world: World) -> None:
    response = await api.post(
        ORGS, json={"name": "Initech", "slug": "initech"}, headers=world.auth(world.platform_admin)
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["slug"], body["status"]) == ("Initech", "initech", "active")


@pytest.mark.parametrize(
    "body",
    [
        {"name": "X", "slug": "Not A Slug"},
        {"name": "", "slug": "ok"},
        {"slug": "missing-name"},
        {"name": "X", "slug": "x", "extra": 1},
    ],
)
async def test_create_organization_validates_input(
    api: AsyncClient, world: World, body: dict[str, object]
) -> None:
    response = await api.post(ORGS, json=body, headers=world.auth(world.platform_admin))

    assert response.status_code == 422


async def test_create_organization_rejects_duplicate_slug(api: AsyncClient, world: World) -> None:
    response = await api.post(
        ORGS, json={"name": "Acme 2", "slug": "acme"}, headers=world.auth(world.platform_admin)
    )

    assert response.status_code == 409


async def test_org_admin_cannot_create_organization(api: AsyncClient, world: World) -> None:
    response = await api.post(
        ORGS, json={"name": "Initech", "slug": "initech"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 403


# --- list


async def test_platform_admin_lists_all_organizations(api: AsyncClient, world: World) -> None:
    response = await api.get(ORGS, headers=world.auth(world.platform_admin))

    assert [o["slug"] for o in response.json()] == ["acme", "globex"]


async def test_member_lists_only_own_organizations(api: AsyncClient, world: World) -> None:
    response = await api.get(ORGS, headers=world.auth(world.a_contributor))

    assert [o["slug"] for o in response.json()] == ["acme"]


async def test_outsider_lists_nothing(api: AsyncClient, world: World) -> None:
    response = await api.get(ORGS, headers=world.auth(world.outsider))

    assert response.json() == []


async def test_list_requires_token(api: AsyncClient) -> None:
    assert (await api.get(ORGS)).status_code == 401


# --- get


async def test_member_gets_organization(api: AsyncClient, world: World) -> None:
    response = await api.get(f"{ORGS}/{world.org_a}", headers=world.auth(world.a_viewer))

    assert response.status_code == 200
    assert response.json()["slug"] == "acme"


async def test_platform_admin_gets_any_organization(api: AsyncClient, world: World) -> None:
    response = await api.get(f"{ORGS}/{world.org_b}", headers=world.auth(world.platform_admin))

    assert response.status_code == 200


async def test_get_organization_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.get(f"{ORGS}/{world.org_b}", headers=world.auth(world.a_admin))

    assert response.status_code == 404
    assert problem_code(response.json()) == "not-found"


async def test_get_organization_bad_id_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(f"{ORGS}/not-a-uuid", headers=world.auth(world.a_admin))

    assert response.status_code == 422


async def test_get_organization_requires_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(f"{ORGS}/{world.org_a}")).status_code == 401


async def test_revoked_member_loses_access(api: AsyncClient, world: World, seed: Seed) -> None:
    await seed._exec(
        "UPDATE role_assignment SET revoked_at = now(), revoked_by = user_id WHERE user_id = :u",
        u=world.a_viewer,
    )

    response = await api.get(f"{ORGS}/{world.org_a}", headers=world.auth(world.a_viewer))

    assert response.status_code == 404


async def test_suspended_organization_blocks_members(
    api: AsyncClient, world: World, seed: Seed
) -> None:
    await seed._exec("UPDATE organization SET status = 'suspended' WHERE id = :id", id=world.org_a)

    member = await api.get(f"{ORGS}/{world.org_a}", headers=world.auth(world.a_admin))
    admin = await api.get(f"{ORGS}/{world.org_a}", headers=world.auth(world.platform_admin))

    assert (member.status_code, admin.status_code) == (403, 200)


# --- update (platform admin only)


async def test_platform_admin_suspends_organization(api: AsyncClient, world: World) -> None:
    response = await api.patch(
        f"{ORGS}/{world.org_a}",
        json={"status": "suspended", "name": "Acme Corp"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["name"]) == ("suspended", "Acme Corp")


async def test_update_organization_validates_status(api: AsyncClient, world: World) -> None:
    response = await api.patch(
        f"{ORGS}/{world.org_a}",
        json={"status": "deleted"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 422


async def test_org_admin_cannot_update_organization(api: AsyncClient, world: World) -> None:
    response = await api.patch(
        f"{ORGS}/{world.org_a}", json={"name": "Mine now"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 403


async def test_update_other_tenant_organization_is_404(api: AsyncClient, world: World) -> None:
    response = await api.patch(
        f"{ORGS}/{world.org_b}", json={"name": "Hijacked"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 404
