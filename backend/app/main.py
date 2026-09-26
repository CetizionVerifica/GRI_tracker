"""FastAPI application factory."""

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name)
    app.state.settings = settings

    register_error_handlers(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for router in MODULE_ROUTERS:
        app.include_router(router)

    return app


app = create_app()
