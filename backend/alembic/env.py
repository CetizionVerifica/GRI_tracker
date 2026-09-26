"""Alembic environment: async engine, URL and metadata taken from the application."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.modules.assistant.models
import app.modules.audit.models
import app.modules.calculation.models
import app.modules.catalog.models
import app.modules.collection.models
import app.modules.reporting.models
import app.modules.tenancy.models
import app.modules.workflow.models  # noqa: F401  (register all models on Base.metadata)
from app.core.config import get_settings
from app.core.db import Base, create_engine

config = context.config
if config.config_file_name is not None:
    # Keep loggers the app already configured when migrations run in-process (e.g. in tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """An explicit sqlalchemy.url (as set by the tests) wins over the settings."""
    if url := config.get_main_option("sqlalchemy.url"):
        return url
    settings = get_settings()
    return settings.migration_database_url or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
