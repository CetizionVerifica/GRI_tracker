import json
import uuid

import pytest
from httpx import AsyncClient


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def test_generates_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert _is_uuid(response.headers["X-Request-ID"])


async def test_propagates_safe_incoming_request_id(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "abc-123_DEF.9"})

    assert response.headers["X-Request-ID"] == "abc-123_DEF.9"


@pytest.mark.parametrize(
    "unsafe",
    ["", "has space", "new\\nline", "<script>", "x" * 129],
    ids=["empty", "space", "escape", "markup", "too-long"],
)
async def test_replaces_unsafe_incoming_request_id(client: AsyncClient, unsafe: str) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": unsafe})

    assert response.headers["X-Request-ID"] != unsafe
    assert _is_uuid(response.headers["X-Request-ID"])


async def test_access_log_is_json_with_request_id(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.get("/health/live", headers={"X-Request-ID": "trace-me"})

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    access = [line for line in lines if line.get("logger") == "app.access"]
    assert access == [
        {
            "event": "request",
            "method": "GET",
            "path": "/health/live",
            "status": 200,
            "duration_ms": access[0]["duration_ms"],
            "request_id": "trace-me",
            "level": "info",
            "logger": "app.access",
            "timestamp": access[0]["timestamp"],
        }
    ]
