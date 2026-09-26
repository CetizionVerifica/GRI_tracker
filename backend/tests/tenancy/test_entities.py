from uuid import UUID

import pytest
from httpx import AsyncClient

from app.core.ids import uuid7
from tests.tenancy.conftest import World

pytestmark = pytest.mark.integration


def url(org: UUID, entity: UUID | None = None) -> str:
    base = f"/api/v1/organizations/{org}/entities"
    return f"{base}/{entity}" if entity else base


# --- create


async def test_org_admin_creates_entity_tree(api: AsyncClient, world: World) -> None:
    headers = world.auth(world.a_admin)
    group = await api.post(
        url(world.org_a),
        json={"code": "GRP", "name": "Acme Group", "kind": "group"},
        headers=headers,
    )
    site = await api.post(
        url(world.org_a),
        json={
            "code": "PUNE",
            "name": "Pune plant",
            "kind": "site",
            "parent_id": group.json()["id"],
        },
        headers=headers,
    )

    assert (group.status_code, site.status_code) == (201, 201)
    assert site.json()["parent_id"] == group.json()["id"]


async def test_platform_admin_creates_entity_in_any_org(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_b),
        json={"code": "HQ", "name": "HQ", "kind": "legal_entity"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"code": "X", "name": "X", "kind": "planet"},
        {"code": "", "name": "X", "kind": "site"},
        {"name": "X", "kind": "site"},
        {"code": "X", "name": "X", "kind": "site", "parent_id": "nope"},
    ],
)
async def test_create_entity_validates_input(
    api: AsyncClient, world: World, body: dict[str, str]
) -> None:
    response = await api.post(url(world.org_a), json=body, headers=world.auth(world.a_admin))

    assert response.status_code == 422


async def test_create_entity_rejects_duplicate_code(api: AsyncClient, world: World) -> None:
    await world.seed.entity(world.org_a, "PUNE")

    response = await api.post(
        url(world.org_a),
        json={"code": "PUNE", "name": "Other", "kind": "site"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


async def test_same_code_is_fine_in_another_org(api: AsyncClient, world: World) -> None:
    await world.seed.entity(world.org_b, "PUNE")

    response = await api.post(
        url(world.org_a),
        json={"code": "PUNE", "name": "Pune", "kind": "site"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201


async def test_create_entity_under_archived_parent_conflicts(
    api: AsyncClient, world: World
) -> None:
    parent = await world.seed.entity(world.org_a, "OLD", archived=True)

    response = await api.post(
        url(world.org_a),
        json={"code": "NEW", "name": "New", "kind": "site", "parent_id": str(parent)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


async def test_parent_from_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await world.seed.entity(world.org_b, "B-HQ")

    response = await api.post(
        url(world.org_a),
        json={"code": "X", "name": "X", "kind": "site", "parent_id": str(foreign)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_non_admin_cannot_create_entity(api: AsyncClient, world: World, role: str) -> None:
    response = await api.post(
        url(world.org_a),
        json={"code": "X", "name": "X", "kind": "site"},
        headers=world.auth(getattr(world, role)),
    )

    assert response.status_code == 403


async def test_create_entity_in_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.post(
        url(world.org_b),
        json={"code": "X", "name": "X", "kind": "site"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


# --- list and get


async def test_members_list_active_entities(api: AsyncClient, world: World) -> None:
    await world.seed.entity(world.org_a, "A1")
    await world.seed.entity(world.org_a, "A0", archived=True)
    await world.seed.entity(world.org_b, "B1")

    active = await api.get(url(world.org_a), headers=world.auth(world.a_viewer))
    everything = await api.get(
        url(world.org_a), params={"include_archived": True}, headers=world.auth(world.a_auditor)
    )

    assert [e["code"] for e in active.json()] == ["A1"]
    assert [e["code"] for e in everything.json()] == ["A0", "A1"]


async def test_list_entities_bad_query_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(
        url(world.org_a), params={"include_archived": "maybe"}, headers=world.auth(world.a_viewer)
    )

    assert response.status_code == 422


async def test_list_entities_requires_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(url(world.org_a))).status_code == 401


async def test_list_entities_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.get(url(world.org_b), headers=world.auth(world.a_admin))

    assert response.status_code == 404


async def test_get_entity(api: AsyncClient, world: World) -> None:
    entity = await world.seed.entity(world.org_a, "A1")

    response = await api.get(url(world.org_a, entity), headers=world.auth(world.a_contributor))

    assert response.status_code == 200
    assert response.json()["code"] == "A1"


async def test_get_entity_unknown_id_is_404(api: AsyncClient, world: World) -> None:
    response = await api.get(url(world.org_a, uuid7()), headers=world.auth(world.a_viewer))

    assert response.status_code == 404


async def test_get_entity_requires_token(api: AsyncClient, world: World) -> None:
    entity = await world.seed.entity(world.org_a, "A1")

    assert (await api.get(url(world.org_a, entity))).status_code == 401


async def test_get_entity_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await world.seed.entity(world.org_b, "B1")

    via_own_org = await api.get(url(world.org_a, foreign), headers=world.auth(world.a_admin))
    via_their_org = await api.get(url(world.org_b, foreign), headers=world.auth(world.a_admin))

    assert (via_own_org.status_code, via_their_org.status_code) == (404, 404)


# --- update


async def test_org_admin_renames_and_moves_entity(api: AsyncClient, world: World) -> None:
    root = await world.seed.entity(world.org_a, "ROOT", kind="group")
    site = await world.seed.entity(world.org_a, "S1")

    response = await api.patch(
        url(world.org_a, site),
        json={"name": "Site one", "parent_id": str(root)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 200
    assert (response.json()["name"], response.json()["parent_id"]) == ("Site one", str(root))


async def test_parent_id_null_makes_entity_a_root(api: AsyncClient, world: World) -> None:
    root = await world.seed.entity(world.org_a, "ROOT", kind="group")
    site = await world.seed.entity(world.org_a, "S1", parent_id=root)

    response = await api.patch(
        url(world.org_a, site), json={"parent_id": None}, headers=world.auth(world.a_admin)
    )

    assert response.json()["parent_id"] is None


async def test_moving_entity_under_its_descendant_conflicts(api: AsyncClient, world: World) -> None:
    root = await world.seed.entity(world.org_a, "ROOT", kind="group")
    child = await world.seed.entity(world.org_a, "CHILD", parent_id=root)
    grandchild = await world.seed.entity(world.org_a, "GRAND", parent_id=child)

    response = await api.patch(
        url(world.org_a, root),
        json={"parent_id": str(grandchild)},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


async def test_archive_requires_no_active_children(api: AsyncClient, world: World) -> None:
    root = await world.seed.entity(world.org_a, "ROOT", kind="group")
    child = await world.seed.entity(world.org_a, "CHILD", parent_id=root)
    headers = world.auth(world.a_admin)

    blocked = await api.patch(url(world.org_a, root), json={"archived": True}, headers=headers)
    await api.patch(url(world.org_a, child), json={"archived": True}, headers=headers)
    allowed = await api.patch(url(world.org_a, root), json={"archived": True}, headers=headers)

    assert (blocked.status_code, allowed.status_code) == (409, 200)
    assert allowed.json()["archived_at"] is not None


async def test_update_entity_validates_input(api: AsyncClient, world: World) -> None:
    entity = await world.seed.entity(world.org_a, "A1")

    response = await api.patch(
        url(world.org_a, entity), json={"kind": "planet"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


async def test_contributor_cannot_update_entity(api: AsyncClient, world: World) -> None:
    entity = await world.seed.entity(world.org_a, "A1")

    response = await api.patch(
        url(world.org_a, entity), json={"name": "X"}, headers=world.auth(world.a_contributor)
    )

    assert response.status_code == 403


async def test_update_entity_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await world.seed.entity(world.org_b, "B1")

    response = await api.patch(
        url(world.org_a, foreign), json={"name": "Mine"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 404
