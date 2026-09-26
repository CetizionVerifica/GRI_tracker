from datetime import date
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.core.ids import uuid7
from tests.world import PASSWORD, World

pytestmark = pytest.mark.integration


def log_url(org: UUID, suffix: str = "") -> str:
    return f"/api/v1/organizations/{org}/audit-log{suffix}"


async def entries(api: AsyncClient, world: World, org: UUID, **params: Any) -> list[dict[str, Any]]:
    response = await api.get(
        log_url(org),
        params=params,
        headers=world.auth(world.a_auditor if org == world.org_a else world.platform_admin),
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def platform_entries(api: AsyncClient, world: World, **params: Any) -> list[dict[str, Any]]:
    response = await api.get(
        "/api/v1/audit-log", params=params, headers=world.auth(world.platform_admin)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- what gets recorded


async def test_unlock_records_reason_actor_and_request_id(api: AsyncClient, world: World) -> None:
    period = await world.seed.period(
        world.org_a, "FY25", date(2025, 1, 1), date(2025, 12, 31), status="locked", by=world.a_admin
    )

    await api.post(
        f"/api/v1/organizations/{world.org_a}/periods/{period}/unlock",
        json={"reason": "Late data from the Pune site"},
        headers={**world.auth(world.a_admin), "X-Request-ID": "req-unlock-1"},
    )

    [entry] = await entries(api, world, world.org_a, action="reporting_period.unlocked")
    assert entry["reason"] == "Late data from the Pune site"
    assert entry["actor_user_id"] == str(world.a_admin)
    assert entry["request_id"] == "req-unlock-1"
    assert entry["before"]["status"] == "locked"
    assert entry["after"] == {"status": "open", "locked_at": None, "locked_by": None}


async def test_updates_record_only_changed_fields(api: AsyncClient, world: World) -> None:
    entity = await world.seed.entity(world.org_a, "PUNE")

    await api.patch(
        f"/api/v1/organizations/{world.org_a}/entities/{entity}",
        json={"name": "Pune plant", "code": "PUNE"},
        headers=world.auth(world.a_admin),
    )

    [entry] = await entries(api, world, world.org_a, action="entity.updated")
    assert (entry["before"], entry["after"]) == ({"name": "PUNE"}, {"name": "Pune plant"})


async def test_failed_changes_leave_no_entry(api: AsyncClient, world: World) -> None:
    await world.seed.entity(world.org_a, "PUNE")

    response = await api.post(
        f"/api/v1/organizations/{world.org_a}/entities",
        json={"code": "PUNE", "name": "Dup", "kind": "site"},
        headers=world.auth(world.a_admin),
    )

    assert response.status_code == 409
    assert await entries(api, world, world.org_a) == []


async def test_user_entries_hold_no_personal_data(api: AsyncClient, world: World) -> None:
    await api.post(
        "/api/v1/users",
        json={
            "email": "priya@acme.test",
            "display_name": "Priya Rao",
            "phone": "+919812345678",
            "password": "a long password 1",
        },
        headers=world.auth(world.platform_admin),
    )

    [entry] = await platform_entries(api, world, action="user.created")
    blob = str(entry)
    assert entry["after"] == {
        "is_active": True,
        "changed_fields": ["display_name", "email", "phone"],
    }
    for value in ("priya@acme.test", "Priya Rao", "+919812345678", "a long password 1"):
        assert value not in blob


async def test_login_logout_and_password_change_are_recorded(
    api: AsyncClient, world: World
) -> None:
    login = await api.post(
        "/api/v1/auth/login", json={"email": "viewer@acme.test", "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    await api.put(
        "/api/v1/auth/me/password",
        json={"current_password": PASSWORD, "new_password": "another long password"},
        headers=headers,
    )
    await api.post("/api/v1/auth/logout", headers=headers)
    await api.post("/api/v1/auth/login", json={"email": "viewer@acme.test", "password": "wrong!"})

    actions = [e["action"] for e in await platform_entries(api, world)]
    assert actions == ["auth.logout", "auth.password_changed", "auth.login"]  # newest first
    assert PASSWORD not in str(await platform_entries(api, world))


async def test_role_grant_and_revoke_are_recorded(api: AsyncClient, world: World) -> None:
    granted = await api.post(
        f"/api/v1/organizations/{world.org_a}/role-assignments",
        json={"email": "nobody@nowhere.test", "role": "viewer"},
        headers=world.auth(world.a_admin),
    )
    await api.post(
        f"/api/v1/organizations/{world.org_a}/role-assignments/{granted.json()['id']}/revoke",
        headers=world.auth(world.a_admin),
    )

    actions = [e["action"] for e in await entries(api, world, world.org_a)]
    assert actions == ["role_assignment.revoked", "role_assignment.granted"]


async def test_organization_created_on_platform_chain(api: AsyncClient, world: World) -> None:
    created = await api.post(
        "/api/v1/organizations",
        json={"name": "Initech", "slug": "initech"},
        headers=world.auth(world.platform_admin),
    )

    [entry] = await platform_entries(api, world, action="organization.created")
    assert entry["target_id"] == created.json()["id"]
    assert entry["after"] == {"name": "Initech", "slug": "initech", "status": "active"}


# --- list endpoint


@pytest.mark.parametrize("role", ["a_admin", "a_auditor", "platform_admin"])
async def test_readers_list_the_org_log(api: AsyncClient, world: World, role: str) -> None:
    response = await api.get(log_url(world.org_a), headers=world.auth(getattr(world, role)))

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_before_seq": None}


async def test_list_pages_newest_first(api: AsyncClient, world: World) -> None:
    for code in ("A", "B", "C"):
        await api.post(
            f"/api/v1/organizations/{world.org_a}/entities",
            json={"code": code, "name": code, "kind": "site"},
            headers=world.auth(world.a_admin),
        )

    first = await api.get(
        log_url(world.org_a), params={"limit": 2}, headers=world.auth(world.a_auditor)
    )
    second = await api.get(
        log_url(world.org_a),
        params={"limit": 2, "before_seq": first.json()["next_before_seq"]},
        headers=world.auth(world.a_auditor),
    )

    assert [e["seq"] for e in first.json()["items"]] == [3, 2]
    assert [e["seq"] for e in second.json()["items"]] == [1]
    assert second.json()["next_before_seq"] is None
    assert first.json()["items"][0]["prev_hash"] == first.json()["items"][1]["row_hash"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"before_seq": 0},
        {"target_id": "nope"},
        {"occurred_from": "2026-02-01T00:00:00Z", "occurred_to": "2026-01-01T00:00:00Z"},
        {"surprise": "x"},
    ],
)
async def test_list_validates_filters(
    api: AsyncClient, world: World, params: dict[str, Any]
) -> None:
    response = await api.get(log_url(world.org_a), params=params, headers=world.auth(world.a_admin))

    assert response.status_code == 422


@pytest.mark.parametrize("role", ["a_contributor", "a_viewer"])
async def test_other_roles_cannot_read_the_log(api: AsyncClient, world: World, role: str) -> None:
    for suffix in ("", "/verify"):
        response = await api.get(
            log_url(world.org_a, suffix), headers=world.auth(getattr(world, role))
        )
        assert response.status_code == 403


async def test_other_tenants_log_is_404(api: AsyncClient, world: World) -> None:
    for suffix in ("", "/verify"):
        response = await api.get(log_url(world.org_b, suffix), headers=world.auth(world.a_admin))
        assert response.status_code == 404


async def test_log_requires_token(api: AsyncClient, world: World) -> None:
    for path in (
        log_url(world.org_a),
        log_url(world.org_a, "/verify"),
        "/api/v1/audit-log",
        "/api/v1/audit-log/verify",
    ):
        assert (await api.get(path)).status_code == 401


# --- verify endpoint


async def test_verify_reports_an_intact_chain(api: AsyncClient, world: World) -> None:
    await api.post(
        f"/api/v1/organizations/{world.org_a}/entities",
        json={"code": "A", "name": "A", "kind": "site"},
        headers=world.auth(world.a_admin),
    )

    response = await api.get(log_url(world.org_a, "/verify"), headers=world.auth(world.a_auditor))

    assert response.json() == {
        "organization_id": str(world.org_a),
        "rows_checked": 1,
        "intact": True,
        "first_broken_seq": None,
    }


async def test_verify_bad_org_id_is_422(api: AsyncClient, world: World) -> None:
    response = await api.get(
        "/api/v1/organizations/nope/audit-log/verify", headers=world.auth(world.a_auditor)
    )

    assert response.status_code == 422


# --- platform chain


async def test_platform_log_is_platform_admin_only(api: AsyncClient, world: World) -> None:
    for role in ("a_admin", "a_auditor", "outsider"):
        for path in ("/api/v1/audit-log", "/api/v1/audit-log/verify"):
            response = await api.get(path, headers=world.auth(getattr(world, role)))
            assert response.status_code == 403


async def test_platform_chain_verifies(api: AsyncClient, world: World) -> None:
    await api.post(
        "/api/v1/organizations",
        json={"name": "Initech", "slug": "initech"},
        headers=world.auth(world.platform_admin),
    )

    response = await api.get("/api/v1/audit-log/verify", headers=world.auth(world.platform_admin))

    assert response.json()["intact"] is True
    assert response.json()["rows_checked"] >= 1


async def test_platform_log_validates_filters(api: AsyncClient, world: World) -> None:
    response = await api.get(
        "/api/v1/audit-log",
        params={"target_id": str(uuid7())[:-1]},
        headers=world.auth(world.platform_admin),
    )

    assert response.status_code == 422
