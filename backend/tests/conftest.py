import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from app.core.config import Settings
from app.core.db import create_engine
from app.main import create_app


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A real PostgreSQL for the whole test session.

    Uses TEST_DATABASE_URL when set (CI's service container), else a testcontainer (needs Docker).
    """
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
async def db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(postgres_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
