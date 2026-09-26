from collections.abc import AsyncIterator, Callable

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import make_client, unused_port


@pytest.fixture
async def live_client(live_settings: Settings) -> AsyncIterator[AsyncClient]:
    async for c in make_client(create_app(live_settings)):
        yield c


@pytest.mark.integration
async def test_live_returns_ok(live_client: AsyncClient) -> None:
    response = await live_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_live_does_not_depend_on_services(client: AsyncClient) -> None:
    # `client` points at unreachable DB, Redis and storage.
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_ready_returns_ok_when_all_dependencies_are_up(live_client: AsyncClient) -> None:
    response = await live_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "storage": "ok"},
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failing_check", "override"),
    [
        (
            "database",
            lambda: {"database_url": f"postgresql+psycopg://x:x@127.0.0.1:{unused_port()}/x"},
        ),
        ("redis", lambda: {"redis_url": f"redis://127.0.0.1:{unused_port()}/0"}),
        ("storage", lambda: {"s3_endpoint_url": f"http://127.0.0.1:{unused_port()}"}),
        ("storage", lambda: {"s3_bucket": "bucket-that-does-not-exist"}),
    ],
    ids=["database-down", "redis-down", "storage-down", "storage-bucket-missing"],
)
async def test_ready_returns_503_when_a_dependency_fails(
    live_settings: Settings,
    failing_check: str,
    override: Callable[[], dict[str, object]],
) -> None:
    settings = live_settings.model_copy(update=override())
    async for client in make_client(create_app(settings)):
        response = await client.get("/health/ready")
        live = await client.get("/health/live")

    expected = {"database": "ok", "redis": "ok", "storage": "ok"} | {failing_check: "fail"}
    assert response.status_code == 503
    assert response.json() == {"status": "fail", "checks": expected}
    assert live.status_code == 200


async def test_ready_does_not_leak_error_details(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "fail",
        "checks": {"database": "fail", "redis": "fail", "storage": "fail"},
    }
