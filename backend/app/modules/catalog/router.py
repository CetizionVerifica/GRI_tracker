"""catalog: HTTP endpoints. Thin: validate, call the service, return."""

from uuid import UUID

from fastapi import APIRouter, status

from app.core.db import DbSession
from app.modules.catalog import service
from app.modules.catalog.schemas import (
    DimensionCreate,
    DimensionOut,
    DimensionValueCreate,
    DimensionValueOut,
    DimensionValueUpdate,
    MetricCreate,
    MetricOut,
    MetricUpdate,
    StandardDetail,
    StandardOut,
)
from app.modules.tenancy.service import CurrentIdentity, CurrentOrgAccess

router = APIRouter(prefix="/api/v1", tags=["catalog"])

ORG = "/organizations/{organization_id}"


# --- standards (global; any signed-in user)


@router.get("/catalog/standards")
async def list_standards(_identity: CurrentIdentity, session: DbSession) -> list[StandardOut]:
    return await service.list_standards(session)


@router.get("/catalog/standards/{standard_id}")
async def get_standard(
    standard_id: UUID, _identity: CurrentIdentity, session: DbSession
) -> StandardDetail:
    return await service.get_standard(session, standard_id)


# --- dimensions (global plus the organization's own)


@router.get(f"{ORG}/dimensions")
async def list_dimensions(
    access: CurrentOrgAccess, session: DbSession, include_retired: bool = False
) -> list[DimensionOut]:
    return await service.list_dimensions(session, access, include_retired=include_retired)


@router.post(f"{ORG}/dimensions", status_code=status.HTTP_201_CREATED)
async def create_dimension(
    body: DimensionCreate, access: CurrentOrgAccess, session: DbSession
) -> DimensionOut:
    return await service.create_dimension(session, access, body)


@router.post(f"{ORG}/dimensions/{{dimension_id}}/values", status_code=status.HTTP_201_CREATED)
async def add_dimension_value(
    dimension_id: UUID, body: DimensionValueCreate, access: CurrentOrgAccess, session: DbSession
) -> DimensionValueOut:
    return await service.add_dimension_value(session, access, dimension_id, body)


@router.patch(f"{ORG}/dimension-values/{{value_id}}")
async def update_dimension_value(
    value_id: UUID, body: DimensionValueUpdate, access: CurrentOrgAccess, session: DbSession
) -> DimensionValueOut:
    return await service.update_dimension_value(session, access, value_id, body)


# --- metrics (global plus the organization's own)


@router.get(f"{ORG}/metrics")
async def list_metrics(
    access: CurrentOrgAccess,
    session: DbSession,
    disclosure_id: UUID | None = None,
    include_retired: bool = False,
) -> list[MetricOut]:
    return await service.list_metrics(
        session, access, disclosure_id=disclosure_id, include_retired=include_retired
    )


@router.post(f"{ORG}/metrics", status_code=status.HTTP_201_CREATED)
async def create_metric(
    body: MetricCreate, access: CurrentOrgAccess, session: DbSession
) -> MetricOut:
    return await service.create_metric(session, access, body)


@router.get(f"{ORG}/metrics/{{metric_id}}")
async def get_metric(metric_id: UUID, access: CurrentOrgAccess, session: DbSession) -> MetricOut:
    return await service.get_metric(session, access, metric_id)


@router.patch(f"{ORG}/metrics/{{metric_id}}")
async def update_metric(
    metric_id: UUID, body: MetricUpdate, access: CurrentOrgAccess, session: DbSession
) -> MetricOut:
    return await service.update_metric(session, access, metric_id, body)
