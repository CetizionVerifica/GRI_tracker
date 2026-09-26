"""audit: HTTP endpoints. Thin: validate, call the service, return."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.db import DbSession
from app.modules.audit import service
from app.modules.audit.schemas import AuditPage, AuditQuery, ChainVerification
from app.modules.tenancy.service import CurrentIdentity, CurrentOrgAccess

router = APIRouter(prefix="/api/v1", tags=["audit"])

ORG = "/organizations/{organization_id}"


@router.get(f"{ORG}/audit-log")
async def list_org_log(
    access: CurrentOrgAccess, session: DbSession, query: Annotated[AuditQuery, Query()]
) -> AuditPage:
    return await service.list_org_log(session, access, query)


@router.get(f"{ORG}/audit-log/verify")
async def verify_org_chain(access: CurrentOrgAccess, session: DbSession) -> ChainVerification:
    return await service.verify_org_chain(session, access)


@router.get("/audit-log")
async def list_platform_log(
    identity: CurrentIdentity, session: DbSession, query: Annotated[AuditQuery, Query()]
) -> AuditPage:
    return await service.list_platform_log(session, identity, query)


@router.get("/audit-log/verify")
async def verify_platform_chain(identity: CurrentIdentity, session: DbSession) -> ChainVerification:
    return await service.verify_platform_chain(session, identity)
