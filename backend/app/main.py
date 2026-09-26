"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.errors import register_error_handlers
from app.core.health import router as health_router
from app.core.logging import RequestIdMiddleware, configure_logging
from app.core.storage import create_s3_client
from app.modules.assistant.router import router as assistant_router
from app.modules.audit.router import router as audit_router
from app.modules.calculation.router import router as calculation_router
from app.modules.catalog.router import router as catalog_router
from app.modules.collection.router import router as collection_router
from app.modules.reporting.router import router as reporting_router
from app.modules.tenancy.router import router as tenancy_router
from app.modules.workflow.router import router as workflow_router

MODULE_ROUTERS = (
    tenancy_router,
    catalog_router,
    audit_router,
    collection_router,
    workflow_router,
    calculation_router,
    reporting_router,
    assistant_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open shared clients on startup and close them on shutdown."""
    settings: Settings = app.state.settings
    async with AsyncExitStack() as stack:
        engine = create_engine(settings.database_url)
        stack.push_async_callback(engine.dispose)
        app.state.db_engine = engine
        app.state.session_factory = create_session_factory(engine)

        redis = Redis.from_url(settings.redis_url)
        stack.push_async_callback(redis.aclose)
        app.state.redis = redis

        app.state.s3 = await stack.enter_async_context(create_s3_client(settings))

        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    for router in MODULE_ROUTERS:
        app.include_router(router)

    return app


app = create_app()
