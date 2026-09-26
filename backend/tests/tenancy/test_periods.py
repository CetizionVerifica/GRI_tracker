from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.core.ids import uuid7
from tests.tenancy.conftest import World

pytestmark = pytest.mark.integration

FY25 = {"name": "FY2025", "start_date": "2025-01-01", "end_date": "2025-12-31"}


def url(org: UUID, period: UUID | None = None, action: str | None = None) -> str:
    path = f"/api/v1/organizations/{org}/periods"
    if period:
        path += f"/{period}"
    if action:
        path += f"/{action}"
    return path


async def fy25(world: World, org: UUID | None = None, status: str = "open") -> UUID:
    return await world.seed.period(
        org or world.org_a,
        "FY2025",
        date(2025, 1, 1),
        date(2025, 12, 31),
        status=status,
        by=world.a_admin,
    )


# --- create


async def test_org_admin_creates_period(api: AsyncClient, world: World) -> None:
    response = await api.post(url(world.org_a), json=FY25, headers=world.auth(world.a_admin))

    assert response.status_code == 201
    assert response.json()["status"] == "open"


@pytest.mark.parametrize(
    "change",
    [
        {"end_date": "2024-12-31"},  # before start
        {"start_date": "not-a-date"},
        {"name": ""},
        {"status": "locked"},  # status can't be set on create
    ],
)
async def test_create_period_validates_input(
    api: AsyncClient, world: World, change: dict[str, str]
) -> None:
    response = await api.post(
        url(world.org_a), json=FY25 | change, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2025-12-31", "2026-12-30"),  # shares one day (end dates are inclusive)
        ("2025-06-01", "2025-06-30"),  # inside
        ("2024-01-01", "2026-12-31"),  # around
    ],
)
async def test_overlapping_periods_conflict(
    api: AsyncClient, world: World, start: str, end: str
) -> None:
    await fy25(world)

    response = await api.post(
        url(world.org_a),
        json={"name": "Other", "start_date": start, "end_date": end},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409
    assert "overlaps" in response.json()["detail"]


async def test_adjacent_period_is_fine(api: AsyncClient, world: World) -> None:
    await fy25(world)

    response = await api.post(
        url(world.org_a),
        json={"name": "FY2026", "start_date": "2026-01-01", "end_date": "2026-12-31"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 201


async def test_other_orgs_periods_do_not_overlap_ours(api: AsyncClient, world: World) -> None:
    await fy25(world, org=world.org_b)

    response = await api.post(url(world.org_a), json=FY25, headers=world.auth(world.a_admin))

    assert response.status_code == 201


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_non_admin_cannot_create_period(api: AsyncClient, world: World, role: str) -> None:
    response = await api.post(url(world.org_a), json=FY25, headers=world.auth(getattr(world, role)))

    assert response.status_code == 403


async def test_create_period_in_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    response = await api.post(url(world.org_b), json=FY25, headers=world.auth(world.a_admin))

    assert response.status_code == 404


# --- list and get


async def test_members_list_periods(api: AsyncClient, world: World) -> None:
    await fy25(world)
    await fy25(world, org=world.org_b)

    response = await api.get(url(world.org_a), headers=world.auth(world.a_viewer))

    assert [p["name"] for p in response.json()] == ["FY2025"]


async def test_list_periods_requires_token(api: AsyncClient, world: World) -> None:
    assert (await api.get(url(world.org_a))).status_code == 401


async def test_list_periods_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    assert (await api.get(url(world.org_b), headers=world.auth(world.a_admin))).status_code == 404


async def test_get_period(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    response = await api.get(url(world.org_a, period), headers=world.auth(world.a_auditor))

    assert response.json()["id"] == str(period)


async def test_get_period_bad_id_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(f"{url(world.org_a)}/nope", headers=world.auth(world.a_admin))

    assert response.status_code == 422


async def test_get_period_requires_token(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    assert (await api.get(url(world.org_a, period))).status_code == 401


async def test_get_period_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await fy25(world, org=world.org_b)

    response = await api.get(url(world.org_a, foreign), headers=world.auth(world.a_admin))

    assert response.status_code == 404


# --- update


async def test_update_open_period(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    response = await api.patch(
        url(world.org_a, period), json={"name": "FY 2025"}, headers=world.auth(world.a_admin)
    )

    assert response.json()["name"] == "FY 2025"


async def test_update_period_cannot_invert_dates(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    response = await api.patch(
        url(world.org_a, period), json={"end_date": "2024-01-01"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 409


async def test_update_period_validates_input(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    response = await api.patch(
        url(world.org_a, period), json={"status": "open"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


@pytest.mark.parametrize("status", ["locked", "published"])
async def test_non_open_period_cannot_be_edited(
    api: AsyncClient, world: World, status: str
) -> None:
    period = await fy25(world, status=status)

    response = await api.patch(
        url(world.org_a, period), json={"name": "Renamed"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 409


async def test_contributor_cannot_update_period(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    response = await api.patch(
        url(world.org_a, period), json={"name": "X"}, headers=world.auth(world.a_contributor)
    )

    assert response.status_code == 403


async def test_update_period_of_other_tenant_is_404(api: AsyncClient, world: World) -> None:
    foreign = await fy25(world, org=world.org_b)

    response = await api.patch(
        url(world.org_a, foreign), json={"name": "X"}, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 404


# --- lock, unlock, publish


async def test_full_lifecycle(api: AsyncClient, world: World) -> None:
    period = await fy25(world)
    headers = world.auth(world.a_admin)

    locked = await api.post(url(world.org_a, period, "lock"), headers=headers)
    unlocked = await api.post(
        url(world.org_a, period, "unlock"), json={"reason": "Late site data"}, headers=headers
    )
    await api.post(url(world.org_a, period, "lock"), headers=headers)
    published = await api.post(url(world.org_a, period, "publish"), headers=headers)

    assert locked.json()["status"] == "locked"
    assert locked.json()["locked_by"] == str(world.a_admin)
    assert (unlocked.json()["status"], unlocked.json()["locked_at"]) == ("open", None)
    assert published.json()["status"] == "published"
    assert published.json()["published_by"] == str(world.a_admin)


async def test_platform_admin_unlocks_period(api: AsyncClient, world: World) -> None:
    period = await fy25(world, status="locked")

    response = await api.post(
        url(world.org_a, period, "unlock"),
        json={"reason": "Correction requested by auditor"},
        headers=world.auth(world.platform_admin),
    )

    assert response.json()["status"] == "open"


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("open", "unlock"),
        ("open", "publish"),
        ("locked", "lock"),
        ("published", "lock"),
        ("published", "unlock"),
        ("published", "publish"),
    ],
)
async def test_invalid_transitions_conflict(
    api: AsyncClient, world: World, status: str, action: str
) -> None:
    period = await fy25(world, status=status)

    response = await api.post(
        url(world.org_a, period, action),
        json={"reason": "because"} if action == "unlock" else None,
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409


@pytest.mark.parametrize("body", [None, {}, {"reason": ""}, {"reason": "ok"}])
async def test_unlock_needs_a_reason(
    api: AsyncClient, world: World, body: dict[str, str] | None
) -> None:
    period = await fy25(world, status="locked")

    response = await api.post(
        url(world.org_a, period, "unlock"), json=body, headers=world.auth(world.a_admin)
    )

    assert response.status_code == 422


@pytest.mark.parametrize("action", ["lock", "unlock", "publish"])
@pytest.mark.parametrize("role", ["a_contributor", "a_viewer", "a_auditor"])
async def test_only_admins_change_status(
    api: AsyncClient, world: World, role: str, action: str
) -> None:
    period = await fy25(world, status="open" if action == "lock" else "locked")

    response = await api.post(
        url(world.org_a, period, action),
        json={"reason": "because"} if action == "unlock" else None,
        headers=world.auth(getattr(world, role)),
    )

    assert response.status_code == 403


@pytest.mark.parametrize("action", ["lock", "unlock", "publish"])
async def test_status_change_in_other_tenant_is_404(
    api: AsyncClient, world: World, action: str
) -> None:
    foreign = await fy25(world, org=world.org_b, status="open" if action == "lock" else "locked")

    response = await api.post(
        url(world.org_a, foreign, action),
        json={"reason": "because"} if action == "unlock" else None,
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 404


async def test_status_change_requires_token(api: AsyncClient, world: World) -> None:
    period = await fy25(world)

    assert (await api.post(url(world.org_a, period, "lock"))).status_code == 401


async def test_unknown_period_is_404(api: AsyncClient, world: World) -> None:
    response = await api.post(url(world.org_a, uuid7(), "lock"), headers=world.auth(world.a_admin))

    assert response.status_code == 404
