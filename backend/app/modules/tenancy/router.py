"""tenancy: HTTP endpoints. Thin: validate, call the service, return."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.db import DbSession
from app.modules.tenancy import service
from app.modules.tenancy.schemas import (
    EntityCreate,
    EntityOut,
    EntityUpdate,
    LoginRequest,
    LoginResponse,
    Me,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    PasswordChange,
    PeriodCreate,
    PeriodOut,
    PeriodUnlock,
    PeriodUpdate,
    RoleAssignmentCreate,
    RoleAssignmentOut,
    UserCreate,
    UserOut,
)
from app.modules.tenancy.service import CurrentIdentity, CurrentOrgAccess

router = APIRouter(prefix="/api/v1", tags=["tenancy"])

ORG = "/organizations/{organization_id}"


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


# --- auth


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    session: DbSession,
    settings: Annotated[Settings, Depends(_settings)],
) -> LoginResponse:
    return await service.login(session, settings, body.email, body.password)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    identity: CurrentIdentity,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    await service.logout(session, identity, credentials.credentials)


@router.get("/auth/me")
async def me(identity: CurrentIdentity, session: DbSession) -> Me:
    return await service.me(session, identity)


@router.put("/auth/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange, identity: CurrentIdentity, session: DbSession
) -> None:
    await service.change_password(session, identity, body.current_password, body.new_password)


# --- users


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, identity: CurrentIdentity, session: DbSession) -> UserOut:
    return UserOut.model_validate(await service.create_user(session, identity, body))


# --- organizations


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreate, identity: CurrentIdentity, session: DbSession
) -> OrganizationOut:
    return OrganizationOut.model_validate(
        await service.create_organization(session, identity, body)
    )


@router.get("/organizations")
async def list_organizations(
    identity: CurrentIdentity, session: DbSession
) -> list[OrganizationOut]:
    organizations = await service.list_organizations(session, identity)
    return [OrganizationOut.model_validate(o) for o in organizations]


@router.get(ORG)
async def get_organization(access: CurrentOrgAccess, session: DbSession) -> OrganizationOut:
    return OrganizationOut.model_validate(await service.get_organization(session, access))


@router.patch(ORG)
async def update_organization(
    body: OrganizationUpdate, access: CurrentOrgAccess, session: DbSession
) -> OrganizationOut:
    return OrganizationOut.model_validate(await service.update_organization(session, access, body))


# --- entities


@router.post(f"{ORG}/entities", status_code=status.HTTP_201_CREATED)
async def create_entity(
    body: EntityCreate, access: CurrentOrgAccess, session: DbSession
) -> EntityOut:
    return EntityOut.model_validate(await service.create_entity(session, access, body))


@router.get(f"{ORG}/entities")
async def list_entities(
    access: CurrentOrgAccess, session: DbSession, include_archived: bool = False
) -> list[EntityOut]:
    entities = await service.list_entities(session, access, include_archived=include_archived)
    return [EntityOut.model_validate(e) for e in entities]


@router.get(f"{ORG}/entities/{{entity_id}}")
async def get_entity(entity_id: UUID, access: CurrentOrgAccess, session: DbSession) -> EntityOut:
    return EntityOut.model_validate(await service.get_entity(session, access, entity_id))


@router.patch(f"{ORG}/entities/{{entity_id}}")
async def update_entity(
    entity_id: UUID, body: EntityUpdate, access: CurrentOrgAccess, session: DbSession
) -> EntityOut:
    return EntityOut.model_validate(await service.update_entity(session, access, entity_id, body))


# --- reporting periods


@router.post(f"{ORG}/periods", status_code=status.HTTP_201_CREATED)
async def create_period(
    body: PeriodCreate, access: CurrentOrgAccess, session: DbSession
) -> PeriodOut:
    return PeriodOut.model_validate(await service.create_period(session, access, body))


@router.get(f"{ORG}/periods")
async def list_periods(access: CurrentOrgAccess, session: DbSession) -> list[PeriodOut]:
    return [PeriodOut.model_validate(p) for p in await service.list_periods(session, access)]


@router.get(f"{ORG}/periods/{{period_id}}")
async def get_period(period_id: UUID, access: CurrentOrgAccess, session: DbSession) -> PeriodOut:
    return PeriodOut.model_validate(await service.get_period(session, access, period_id))


@router.patch(f"{ORG}/periods/{{period_id}}")
async def update_period(
    period_id: UUID, body: PeriodUpdate, access: CurrentOrgAccess, session: DbSession
) -> PeriodOut:
    return PeriodOut.model_validate(await service.update_period(session, access, period_id, body))


@router.post(f"{ORG}/periods/{{period_id}}/lock")
async def lock_period(period_id: UUID, access: CurrentOrgAccess, session: DbSession) -> PeriodOut:
    return PeriodOut.model_validate(await service.lock_period(session, access, period_id))


@router.post(f"{ORG}/periods/{{period_id}}/unlock")
async def unlock_period(
    period_id: UUID, body: PeriodUnlock, access: CurrentOrgAccess, session: DbSession
) -> PeriodOut:
    return PeriodOut.model_validate(
        await service.unlock_period(session, access, period_id, body.reason)
    )


@router.post(f"{ORG}/periods/{{period_id}}/publish")
async def publish_period(
    period_id: UUID, access: CurrentOrgAccess, session: DbSession
) -> PeriodOut:
    return PeriodOut.model_validate(await service.publish_period(session, access, period_id))


# --- role assignments


@router.post(f"{ORG}/role-assignments", status_code=status.HTTP_201_CREATED)
async def grant_role(
    body: RoleAssignmentCreate, access: CurrentOrgAccess, session: DbSession
) -> RoleAssignmentOut:
    return await service.grant_role(session, access, body)


@router.get(f"{ORG}/role-assignments")
async def list_role_assignments(
    access: CurrentOrgAccess, session: DbSession, include_revoked: bool = False
) -> list[RoleAssignmentOut]:
    return await service.list_role_assignments(session, access, include_revoked=include_revoked)


@router.post(f"{ORG}/role-assignments/{{assignment_id}}/revoke")
async def revoke_role(
    assignment_id: UUID, access: CurrentOrgAccess, session: DbSession
) -> RoleAssignmentOut:
    return await service.revoke_role(session, access, assignment_id)
