import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from app.core.config import Settings
from app.core.db import create_engine
from app.main import create_app

MINIO_IMAGE = (
    "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"  # keep in sync with infra/docker-compose.yml
)
TEST_BUCKET = "gri-kpi-test"
BACKEND_DIR = Path(__file__).resolve().parent.parent

# Test-only login role in gri_app: like the real app, it is subject to row-level security.
APP_TEST_ROLE = "gri_api_test"
APP_TEST_PASSWORD = "gri_api_test"  # noqa: S105  (throwaway test database)


def unused_port() -> int:
    """A local port with nothing listening on it, for simulating an unreachable dependency."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


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
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def minio() -> Iterator[MinioContainer]:
    """MinIO with the test bucket already created."""
    container = MinioContainer(MINIO_IMAGE)
    # Current MinIO releases only read the ROOT_* variables.
    container.with_env("MINIO_ROOT_USER", container.access_key)
    container.with_env("MINIO_ROOT_PASSWORD", container.secret_key)
    with container:
        container.get_client().make_bucket(TEST_BUCKET)
        yield container


@pytest.fixture(scope="session")
async def db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    """Connects as the schema owner (bypasses row-level security). Use app_engine for app tests."""
    engine = create_engine(postgres_url)
    yield engine
    await engine.dispose()


def alembic_config(database_url: str) -> Config:
    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session")
def migrated_postgres_url(postgres_url: str) -> str:
    """The owner URL of a database migrated to head. Sync, because Alembic runs its own loop."""
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture(scope="session")
def app_database_url(migrated_postgres_url: str) -> str:
    """URL for a non-superuser login in gri_app, as the running application would use."""
    engine = create_sync_engine(migrated_postgres_url)
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_TEST_ROLE}
        ).first()
        if not exists:
            conn.execute(
                text(
                    f"CREATE ROLE {APP_TEST_ROLE} LOGIN PASSWORD '{APP_TEST_PASSWORD}'"
                    " IN ROLE gri_app"
                )
            )
    engine.dispose()
    url = make_url(migrated_postgres_url).set(username=APP_TEST_ROLE, password=APP_TEST_PASSWORD)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
async def app_engine(app_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(app_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    """Settings with every dependency unreachable; for tests that don't need real services."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"postgresql+psycopg://gri:gri@127.0.0.1:{unused_port()}/gri",
        redis_url=f"redis://127.0.0.1:{unused_port()}/0",
        s3_endpoint_url=f"http://127.0.0.1:{unused_port()}",
        s3_bucket=TEST_BUCKET,
        health_check_timeout_seconds=2,
    )


@pytest.fixture
def live_settings(
    settings: Settings, postgres_url: str, redis_url: str, minio: MinioContainer
) -> Settings:
    """Settings pointing at the real PostgreSQL, Redis and MinIO containers."""
    host = minio.get_container_host_ip()
    port = minio.get_exposed_port(minio.port)
    return settings.model_copy(
        update={
            "database_url": postgres_url,
            "redis_url": redis_url,
            "s3_endpoint_url": f"http://{host}:{port}",
            "s3_access_key": minio.access_key,
            "s3_secret_key": SecretStr(minio.secret_key),  # model_copy skips validation
        }
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


async def make_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client for `app`, with its lifespan (startup/shutdown) running around it."""
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            # Unhandled errors become 500 responses, as they would behind uvicorn.
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        yield client


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async for c in make_client(app):
        yield c
