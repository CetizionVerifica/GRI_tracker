from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from tests.conftest import make_client

PROBLEM_JSON = "application/problem+json"


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """App with extra routes that raise each kind of error."""
    app: FastAPI = create_app(settings)

    @app.get("/_test/not-found")
    async def raise_not_found() -> None:
        raise NotFoundError("Data point 42 does not exist.")

    @app.get("/_test/boom")
    async def raise_unexpected() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/_test/typed")
    async def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    async for c in make_client(app):
        yield c


async def test_app_error_is_problem_json(client: AsyncClient) -> None:
    response = await client.get("/_test/not-found", headers={"X-Request-ID": "req-1"})

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "type": "urn:gri-kpi:problem:not-found",
        "title": "Not found",
        "status": 404,
        "detail": "Data point 42 does not exist.",
        "instance": "/_test/not-found",
        "request_id": "req-1",
    }


async def test_unknown_route_is_problem_json(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist", headers={"X-Request-ID": "req-2"})

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "type": "urn:gri-kpi:problem:not-found",
        "title": "Not Found",
        "status": 404,
        "instance": "/does-not-exist",
        "request_id": "req-2",
    }


async def test_wrong_method_is_problem_json_with_allow_header(client: AsyncClient) -> None:
    response = await client.post("/health/live")

    assert response.status_code == 405
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.headers["allow"] == "GET"
    assert response.json()["type"] == "urn:gri-kpi:problem:method-not-allowed"


async def test_validation_error_lists_errors(client: AsyncClient) -> None:
    response = await client.get("/_test/typed", params={"limit": "many"})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"] == "urn:gri-kpi:problem:validation-error"
    assert body["status"] == 422
    assert body["errors"] == [
        {
            "loc": ["query", "limit"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "type": "int_parsing",
        }
    ]


async def test_unexpected_error_is_generic_500(client: AsyncClient) -> None:
    response = await client.get("/_test/boom", headers={"X-Request-ID": "req-3"})

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.headers["X-Request-ID"] == "req-3"
    assert response.json() == {
        "type": "urn:gri-kpi:problem:internal-error",
        "title": "Internal server error",
        "status": 500,
        "instance": "/_test/boom",
        "request_id": "req-3",
    }
    assert "secret internal detail" not in response.text
