from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.core.ids import uuid7
from tests.catalog.conftest import Catalog
from tests.world import World

pytestmark = pytest.mark.integration


def org_url(org: UUID, path: str) -> str:
    return f"/api/v1/organizations/{org}/{path}"


def new_metric(catalog: Catalog, **changes: Any) -> dict[str, Any]:
    return {
        "disclosure_id": str(catalog.disclosure_1),
        "code": "custom.fleet_fuel",
        "name": "Fleet fuel",
        "data_type": "decimal",
        "unit": "L",
        "requirement": "optional",
        "validation_rules": {"minimum": "0"},
        "dimensions": [{"dimension_id": str(catalog.site), "is_required": True}],
    } | changes


async def create_metric(api: AsyncClient, world: World, catalog: Catalog, org: UUID) -> str:
    admin = world.a_admin if org == world.org_a else world.b_admin
    response = await api.post(
        org_url(org, "metrics"), json=new_metric(catalog), headers=world.auth(admin)
    )
    assert response.status_code == 201, response.text
    metric_id: str = response.json()["id"]
    return metric_id


# --- standards


async def test_any_user_lists_standards(api: AsyncClient, world: World, catalog: Catalog) -> None:
    response = await api.get("/api/v1/catalog/standards", headers=world.auth(world.outsider))

    assert response.status_code == 200
    assert [(s["code"], s["version"]) for s in response.json()] == [("TEST 900", "2020")]


async def test_get_standard_with_disclosures(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.get(
        f"/api/v1/catalog/standards/{catalog.standard}", headers=world.auth(world.a_viewer)
    )

    assert [d["code"] for d in response.json()["disclosures"]] == ["900-1", "900-2"]


async def test_get_unknown_standard(api: AsyncClient, world: World) -> None:
    unknown = await api.get(
        f"/api/v1/catalog/standards/{uuid7()}", headers=world.auth(world.a_viewer)
    )
    malformed = await api.get("/api/v1/catalog/standards/nope", headers=world.auth(world.a_viewer))

    assert (unknown.status_code, malformed.status_code) == (404, 422)


async def test_standards_require_token(api: AsyncClient) -> None:
    assert (await api.get("/api/v1/catalog/standards")).status_code == 401


# --- metrics: list and get


async def test_members_see_global_and_own_metrics_only(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    await create_metric(api, world, catalog, world.org_b)
    own = await create_metric(api, world, catalog, world.org_a)

    response = await api.get(org_url(world.org_a, "metrics"), headers=world.auth(world.a_viewer))

    rows = {m["code"]: m for m in response.json()}
    assert set(rows) == {
        "test900.1.gross",
        "test900.1.method",
        "test900.2.sites",
        "custom.fleet_fuel",
    }
    assert rows["custom.fleet_fuel"]["id"] == own
    assert rows["custom.fleet_fuel"]["is_custom"] is True
    gross = rows["test900.1.gross"]
    assert (gross["unit"], gross["requirement"], gross["validation_rules"]) == (
        "t",
        "required",
        {"minimum": "0", "maximum": "1000000.5"},
    )
    assert gross["dimensions"] == [
        {
            "dimension_id": str(catalog.test_gas),
            "code": "test_gas",
            "name": "Test gas",
            "is_required": True,
        }
    ]


async def test_filter_metrics_by_disclosure(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.get(
        org_url(world.org_a, "metrics"),
        params={"disclosure_id": str(catalog.disclosure_2)},
        headers=world.auth(world.a_viewer),
    )

    assert [m["code"] for m in response.json()] == ["test900.2.sites"]


async def test_list_metrics_bad_filter_is_422(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.get(
        org_url(world.org_a, "metrics"),
        params={"disclosure_id": "nope"},
        headers=world.auth(world.a_viewer),
    )

    assert response.status_code == 422


async def test_list_metrics_requires_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(org_url(world.org_a, "metrics"))).status_code == 401


async def test_list_metrics_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.get(org_url(world.org_b, "metrics"), headers=world.auth(world.a_admin))

    assert response.status_code == 404


async def test_get_metric(api: AsyncClient, world: World, catalog: Catalog) -> None:
    response = await api.get(
        org_url(world.org_a, f"metrics/{catalog.method}"), headers=world.auth(world.a_auditor)
    )

    assert response.json()["validation_rules"] == {"choices": ["measured", "estimated"]}


async def test_get_other_tenants_custom_metric_is_404(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    foreign = await create_metric(api, world, catalog, world.org_b)

    response = await api.get(
        org_url(world.org_a, f"metrics/{foreign}"), headers=world.auth(world.a_admin)
    )

    assert response.status_code == 404


async def test_get_metric_requires_token(api: AsyncClient, world: World, catalog: Catalog) -> None:
    assert (await api.get(org_url(world.org_a, f"metrics/{catalog.gross}"))).status_code == 401


async def test_get_metric_bad_id_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(
        org_url(world.org_a, "metrics/nope"), headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


# --- metrics: create


async def test_org_admin_creates_custom_metric(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog),
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["code"], body["unit"], body["is_custom"]) == ("custom.fleet_fuel", "L", True)
    assert body["dimensions"][0]["code"] == "site"


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "fleet_fuel"},  # missing the custom. prefix
        {"unit": "parsecs_of_joy"},
        {"unit": None},
        {"data_type": "text"},  # text metrics have no unit
        {"validation_rules": {"minimum": 0.5}},  # float
        {"validation_rules": {"choices": ["a"]}},  # choices on a decimal metric
        {"requirement": "mandatory"},
        {"dimensions": [{"dimension_id": str(uuid7())}, {"dimension_id": str(uuid7())}] * 2},
        {"surprise": True},
    ],
)
async def test_create_metric_validates_input(
    api: AsyncClient, world: World, catalog: Catalog, changes: dict[str, Any]
) -> None:
    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog, **changes),
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 422


async def test_create_metric_duplicate_code_conflicts(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    await create_metric(api, world, catalog, world.org_a)

    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog),
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


async def test_same_custom_code_in_two_orgs_is_fine(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    await create_metric(api, world, catalog, world.org_a)
    await create_metric(api, world, catalog, world.org_b)


async def test_create_metric_unknown_disclosure_is_404(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog, disclosure_id=str(uuid7())),
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_create_metric_with_other_tenants_dimension_is_404(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    foreign = await api.post(
        org_url(world.org_b, "dimensions"),
        json={"code": "custom.plant", "name": "Plant"},
        headers=world.auth(world.b_admin),
    )

    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog, dimensions=[{"dimension_id": foreign.json()["id"]}]),
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_non_admin_cannot_create_metric(
    api: AsyncClient, world: World, catalog: Catalog, role: str
) -> None:
    response = await api.post(
        org_url(world.org_a, "metrics"),
        json=new_metric(catalog),
        headers=world.auth(getattr(world, role)),
    )

    assert response.status_code == 403


async def test_create_metric_in_other_tenant_is_404(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.post(
        org_url(world.org_b, "metrics"), json=new_metric(catalog), headers=world.auth(world.a_admin)
    )

    assert response.status_code == 404


async def test_create_metric_requires_token(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.post(org_url(world.org_a, "metrics"), json=new_metric(catalog))

    assert response.status_code == 401


# --- metrics: update


async def test_org_admin_renames_and_retires_custom_metric(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    metric = await create_metric(api, world, catalog, world.org_a)
    headers = world.auth(world.a_admin)

    response = await api.patch(
        org_url(world.org_a, f"metrics/{metric}"),
        json={"name": "Fleet diesel", "retired": True},
        headers=headers,
    )
    listed = await api.get(org_url(world.org_a, "metrics"), headers=headers)

    assert response.status_code == 200
    assert response.json()["name"] == "Fleet diesel"
    assert response.json()["retired_at"] is not None
    assert "custom.fleet_fuel" not in {m["code"] for m in listed.json()}


@pytest.mark.parametrize("body", [{"unit": "kg"}, {"data_type": "text"}, {"requirement": "x"}])
async def test_update_metric_rejects_structural_or_bad_fields(
    api: AsyncClient, world: World, catalog: Catalog, body: dict[str, str]
) -> None:
    metric = await create_metric(api, world, catalog, world.org_a)

    response = await api.patch(
        org_url(world.org_a, f"metrics/{metric}"), json=body, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


async def test_global_metric_is_read_only(api: AsyncClient, world: World, catalog: Catalog) -> None:
    response = await api.patch(
        org_url(world.org_a, f"metrics/{catalog.gross}"),
        json={"name": "Mine"},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 403


async def test_contributor_cannot_update_metric(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    metric = await create_metric(api, world, catalog, world.org_a)

    response = await api.patch(
        org_url(world.org_a, f"metrics/{metric}"),
        json={"name": "X"},
        headers=world.auth(world.a_contributor),
    )

    assert response.status_code == 403


async def test_update_other_tenants_metric_is_404(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    foreign = await create_metric(api, world, catalog, world.org_b)

    response = await api.patch(
        org_url(world.org_a, f"metrics/{foreign}"),
        json={"name": "Mine"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


# --- dimensions


async def test_org_admin_creates_dimension_with_values(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    response = await api.post(
        org_url(world.org_a, "dimensions"),
        json={
            "code": "custom.shift",
            "name": "Shift",
            "values": [
                {"code": "custom.day", "label": "Day"},
                {"code": "custom.night", "label": "Night"},
            ],
        },
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201
    assert [v["code"] for v in response.json()["values"]] == ["custom.day", "custom.night"]


async def test_tenant_values_on_global_dimension_stay_private(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    added = await api.post(
        org_url(world.org_a, f"dimensions/{catalog.site}/values"),
        json={"code": "custom.pune", "label": "Pune plant"},
        headers=world.auth(world.a_admin),
    )

    ours = await api.get(org_url(world.org_a, "dimensions"), headers=world.auth(world.a_viewer))
    theirs = await api.get(org_url(world.org_b, "dimensions"), headers=world.auth(world.b_admin))

    assert added.status_code == 201

    def site_values(response_json: list[dict[str, Any]]) -> list[str]:
        site = next(d for d in response_json if d["code"] == "site")
        return [v["code"] for v in site["values"]]

    assert site_values(ours.json()) == ["custom.pune"]
    assert site_values(theirs.json()) == []


@pytest.mark.parametrize(
    "body",
    [
        {"code": "shift", "name": "Shift"},  # missing the custom. prefix
        {"code": "custom.shift", "name": ""},
        {"code": "custom.shift", "name": "S", "values": [{"code": "custom.a", "label": "A"}] * 2},
    ],
)
async def test_create_dimension_validates_input(
    api: AsyncClient, world: World, body: dict[str, Any]
) -> None:
    response = await api.post(
        org_url(world.org_a, "dimensions"), json=body, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


async def test_create_dimension_duplicate_code_conflicts(api: AsyncClient, world: World) -> None:
    body = {"code": "custom.shift", "name": "Shift"}
    headers = world.auth(world.a_admin)

    await api.post(org_url(world.org_a, "dimensions"), json=body, headers=headers)
    again = await api.post(org_url(world.org_a, "dimensions"), json=body, headers=headers)

    assert again.status_code == 409


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_non_admin_cannot_create_dimension(api: AsyncClient, world: World, role: str) -> None:
    response = await api.post(
        org_url(world.org_a, "dimensions"),
        json={"code": "custom.shift", "name": "Shift"},
        headers=world.auth(getattr(world, role)),
    )

    assert response.status_code == 403


async def test_dimensions_in_other_tenant_are_404(api: AsyncClient, world: World) -> None:
    listed = await api.get(org_url(world.org_b, "dimensions"), headers=world.auth(world.a_admin))
    created = await api.post(
        org_url(world.org_b, "dimensions"),
        json={"code": "custom.shift", "name": "Shift"},
        headers=world.auth(world.a_admin),
    )

    assert (listed.status_code, created.status_code) == (404, 404)


async def test_dimensions_require_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(org_url(world.org_a, "dimensions"))).status_code == 401


async def test_add_value_to_other_tenants_dimension_is_404(api: AsyncClient, world: World) -> None:
    foreign = await api.post(
        org_url(world.org_b, "dimensions"),
        json={"code": "custom.plant", "name": "Plant"},
        headers=world.auth(world.b_admin),
    )

    response = await api.post(
        org_url(world.org_a, f"dimensions/{foreign.json()['id']}/values"),
        json={"code": "custom.x", "label": "X"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_add_value_validates_and_checks_role(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    path = org_url(world.org_a, f"dimensions/{catalog.site}/values")

    bad = await api.post(
        path, json={"code": "pune", "label": "Pune"}, headers=world.auth(world.a_admin)
    )
    viewer = await api.post(
        path, json={"code": "custom.pune", "label": "Pune"}, headers=world.auth(world.a_viewer)
    )
    anonymous = await api.post(path, json={"code": "custom.pune", "label": "Pune"})

    assert (bad.status_code, viewer.status_code, anonymous.status_code) == (422, 403, 401)


async def test_update_own_value_but_not_global_ones(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    headers = world.auth(world.a_admin)
    own = await api.post(
        org_url(world.org_a, f"dimensions/{catalog.site}/values"),
        json={"code": "custom.pune", "label": "Pune"},
        headers=headers,
    )

    renamed = await api.patch(
        org_url(world.org_a, f"dimension-values/{own.json()['id']}"),
        json={"label": "Pune plant", "retired": True},
        headers=headers,
    )
    global_value = await api.patch(
        org_url(world.org_a, f"dimension-values/{catalog.gas_a}"),
        json={"label": "Mine"},
        headers=headers,
    )

    assert renamed.status_code == 200
    assert (renamed.json()["label"], renamed.json()["retired_at"] is not None) == (
        "Pune plant",
        True,
    )
    assert global_value.status_code == 403


async def test_update_value_validates_role_and_tenant(
    api: AsyncClient, world: World, catalog: Catalog
) -> None:
    foreign = await api.post(
        org_url(world.org_b, f"dimensions/{catalog.site}/values"),
        json={"code": "custom.x", "label": "X"},
        headers=world.auth(world.b_admin),
    )
    own = await api.post(
        org_url(world.org_a, f"dimensions/{catalog.site}/values"),
        json={"code": "custom.pune", "label": "Pune"},
        headers=world.auth(world.a_admin),
    )
    own_path = org_url(world.org_a, f"dimension-values/{own.json()['id']}")

    bad = await api.patch(own_path, json={"label": ""}, headers=world.auth(world.a_admin))
    viewer = await api.patch(own_path, json={"label": "X"}, headers=world.auth(world.a_viewer))
    other = await api.patch(
        org_url(world.org_a, f"dimension-values/{foreign.json()['id']}"),
        json={"label": "Mine"},
        headers=world.auth(world.a_admin),
    )

    assert (bad.status_code, viewer.status_code, other.status_code) == (422, 403, 404)
