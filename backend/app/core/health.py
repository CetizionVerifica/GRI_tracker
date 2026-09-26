"""Liveness and readiness probes.

- `/health/live`: the process is up and serving. Never touches dependencies.
- `/health/ready`: PostgreSQL, Redis and object storage are all reachable (503 otherwise).
"""

import asyncio
from collections.abc import Awaitable
from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from types_aiobotocore_s3 import S3Client

from app.core.config import Settings

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

CheckStatus = Literal["ok", "fail"]


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


async def _check_database(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


async def _check_storage(s3: S3Client, bucket: str) -> None:
    await s3.head_bucket(Bucket=bucket)


async def _run_check(name: str, check: Awaitable[None], limit_seconds: float) -> CheckStatus:
    try:
        async with asyncio.timeout(limit_seconds):
            await check
    except Exception as exc:
        # Details stay in the logs; the probe response only says which dependency failed.
        logger.warning("readiness_check_failed", check=name, error=repr(exc))
        return "fail"
    return "ok"


@router.get("/live")
async def live() -> LiveResponse:
    return LiveResponse(status="ok")


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    state = request.app.state
    settings: Settings = state.settings
    engine: AsyncEngine = state.db_engine
    redis: Redis = state.redis
    s3: S3Client = state.s3
    timeout = settings.health_check_timeout_seconds

    names = ("database", "redis", "storage")
    results = await asyncio.gather(
        _run_check("database", _check_database(engine), timeout),
        _run_check("redis", _check_redis(redis), timeout),
        _run_check("storage", _check_storage(s3, settings.s3_bucket), timeout),
    )
    checks = dict(zip(names, results, strict=True))

    if all(result == "ok" for result in results):
        return ReadyResponse(status="ok", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="fail", checks=checks)
