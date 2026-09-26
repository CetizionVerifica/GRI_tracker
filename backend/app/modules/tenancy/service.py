"""tenancy: business logic and the module's public interface for other modules.

Other modules authenticate and authorize through `CurrentIdentity` and `CurrentOrgAccess`.
Permission checks happen here first; row-level security and triggers in the database are the
backstop, so a missed check fails closed instead of leaking data.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    Identity,
    OrgAccess,
    OrgRole,
    hash_password,
    new_session_token,
    password_needs_rehash,
    token_digest,
    verify_password,
)
from app.core.config import Settings
from app.core.db import DbSession, RequestContext, bind_request_context, flush_or_conflict
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
)
from app.modules.audit import service as audit
from app.modules.tenancy import repository
from app.modules.tenancy.models import (
    AppUser,
    Entity,
    Organization,
    OrganizationStatus,
    PeriodStatus,
    PlatformRole,
    PlatformRoleAssignment,
    ReportingPeriod,
    RoleAssignment,
)
from app.modules.tenancy.schemas import (
    EntityCreate,
    EntityUpdate,
    LoginResponse,
    Me,
    Membership,
    OrganizationCreate,
    OrganizationUpdate,
    PeriodCreate,
    PeriodUpdate,
    RoleAssignmentCreate,
    RoleAssignmentOut,
    UserCreate,
)

_INVALID_LOGIN = "Invalid email or password."
# Fields recorded in the audit log. Personal data (email, phone, display name) is never recorded
# by value: user changes log only the names of the fields that changed.
_ORG_FIELDS = ("name", "slug", "status")
_ENTITY_FIELDS = ("parent_id", "code", "name", "kind", "archived_at")
_PERIOD_FIELDS = (
    "name",
    "start_date",
    "end_date",
    "status",
    "locked_at",
    "locked_by",
    "published_at",
    "published_by",
)
_USER_PERSONAL_FIELDS = ("email", "display_name", "phone")
_CONSTRAINT_MESSAGES = {
    "uq_organization_slug": "An organization with this slug already exists.",
    "uq_app_user_email_lower": "A user with this email already exists.",
    "uq_entity_organization_id_code": "An entity with this code already exists.",
    "entity_no_cycle": "An entity cannot be moved under itself or one of its descendants.",
    "uq_reporting_period_organization_id_name": "A reporting period with this name exists.",
    "ex_reporting_period_no_overlap": "The period overlaps another reporting period.",
    "uq_role_assignment_active": "The user already has this role.",
}


async def _flush(session: AsyncSession) -> None:
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)


def _require_role(access: OrgAccess, *roles: OrgRole) -> None:
    if not access.has_any_role(*roles):
        raise PermissionDeniedError("Your role does not allow this action.")


def _require_platform_admin(is_platform_admin: bool) -> None:
    if not is_platform_admin:
        raise PermissionDeniedError("Only a platform admin can do this.")


async def _record_update(
    session: AsyncSession,
    access: OrgAccess,
    table: str,
    target_id: UUID,
    before: dict[str, object],
    obj: object,
    fields: tuple[str, ...],
    action: str | None = None,
    reason: str | None = None,
) -> None:
    """Record the fields that changed on `obj` (nothing, if none did)."""
    changed_before, changed_after = audit.diff(before, audit.snapshot(obj, fields))
    if not changed_after:
        return
    await audit.record(
        session,
        organization_id=access.organization_id,
        actor_user_id=access.user_id,
        action=action or f"{table}.updated",
        target_table=table,
        target_id=target_id,
        before=changed_before,
        after=changed_after,
        reason=reason,
    )


async def _record_created(
    session: AsyncSession, access: OrgAccess, table: str, obj: Any, fields: tuple[str, ...]
) -> None:
    await audit.record(
        session,
        organization_id=access.organization_id,
        actor_user_id=access.user_id,
        action=f"{table}.created",
        target_table=table,
        target_id=obj.id,
        after=audit.snapshot(obj, fields),
    )


# --- dependencies

_bearer = HTTPBearer(auto_error=False)


async def current_identity(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Identity:
    """Resolve the bearer token and scope the session to the caller (no organization yet)."""
    if credentials is None:
        raise UnauthenticatedError("Missing bearer token.")
    resolved = await repository.resolve_session(session, token_digest(credentials.credentials))
    if resolved is None:
        raise UnauthenticatedError("Invalid or expired token.")
    identity = Identity(resolved.user_id, resolved.is_platform_admin)
    await bind_request_context(
        session, RequestContext(None, identity.user_id, identity.is_platform_admin)
    )
    return identity


CurrentIdentity = Annotated[Identity, Depends(current_identity)]


async def org_access(
    organization_id: UUID, identity: CurrentIdentity, session: DbSession
) -> OrgAccess:
    """Resolve the caller's access to the organization in the path, then scope the session to it.

    Non-members get 404, so other organizations' existence is not revealed.
    """
    roles = await repository.active_roles(session, organization_id, identity.user_id)
    if not roles and not identity.is_platform_admin:
        raise NotFoundError("Organization not found.")
    await bind_request_context(
        session, RequestContext(organization_id, identity.user_id, identity.is_platform_admin)
    )
    organization = await repository.get_organization(session, organization_id)
    if organization is None:
        raise NotFoundError("Organization not found.")
    if organization.status == OrganizationStatus.SUSPENDED and not identity.is_platform_admin:
        raise PermissionDeniedError("This organization is suspended.")
    return OrgAccess(
        organization_id, identity.user_id, identity.is_platform_admin, frozenset(roles)
    )


CurrentOrgAccess = Annotated[OrgAccess, Depends(org_access)]


# --- auth


async def login(
    session: AsyncSession, settings: Settings, email: str, password: str
) -> LoginResponse:
    record = await repository.login_lookup(session, email)
    password_hash = record.password_hash if record else None
    # Always verify (against a dummy hash if needed) so response time doesn't reveal accounts.
    valid = await asyncio.to_thread(verify_password, password_hash, password)
    if record is None or password_hash is None or not valid or not record.is_active:
        raise UnauthenticatedError(_INVALID_LOGIN)

    await bind_request_context(session, RequestContext(None, record.user_id))
    if password_needs_rehash(password_hash):
        new_hash = await asyncio.to_thread(hash_password, password)
        await repository.set_password(session, record.user_id, new_hash)

    token, digest = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.session_ttl_minutes)
    repository.add_auth_session(session, record.user_id, digest, expires_at)
    await session.flush()
    await audit.record(
        session,
        organization_id=None,
        actor_user_id=record.user_id,
        action="auth.login",
        target_table="app_user",
        target_id=record.user_id,
    )
    return LoginResponse(access_token=token, expires_at=expires_at)


async def logout(session: AsyncSession, identity: Identity, token: str) -> None:
    auth_session = await repository.get_auth_session(session, identity.user_id, token_digest(token))
    if auth_session is not None and auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(UTC)
        await session.flush()
        await audit.record(
            session,
            organization_id=None,
            actor_user_id=identity.user_id,
            action="auth.logout",
            target_table="app_user",
            target_id=identity.user_id,
        )


async def me(session: AsyncSession, identity: Identity) -> Me:
    user = await repository.get_user(session, identity.user_id)
    if user is None:  # resolved from a valid session, so it exists
        raise NotFoundError("User not found.")
    memberships = [
        Membership(
            organization_id=assignment.organization_id,
            organization_name=organization_name,
            role=OrgRole(assignment.role),
            entity_id=assignment.entity_id,
        )
        for assignment, organization_name in await repository.memberships_of_user(
            session, identity.user_id
        )
    ]
    return Me(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        is_platform_admin=identity.is_platform_admin,
        memberships=memberships,
    )


async def change_password(
    session: AsyncSession, identity: Identity, current_password: str, new_password: str
) -> None:
    user = await repository.get_user(session, identity.user_id)
    record = await repository.login_lookup(session, user.email) if user else None
    stored = record.password_hash if record else None
    if not await asyncio.to_thread(verify_password, stored, current_password):
        raise PermissionDeniedError("The current password is incorrect.")
    new_hash = await asyncio.to_thread(hash_password, new_password)
    await repository.set_password(session, identity.user_id, new_hash)
    await audit.record(
        session,
        organization_id=None,
        actor_user_id=identity.user_id,
        action="auth.password_changed",
        target_table="app_user",
        target_id=identity.user_id,
    )


# --- users


async def _create_user(
    session: AsyncSession, data: UserCreate, actor_user_id: UUID | None
) -> AppUser:
    if await repository.email_taken(session, data.email):
        raise ConflictError(_CONSTRAINT_MESSAGES["uq_app_user_email_lower"])
    user = AppUser(email=data.email, display_name=data.display_name, phone=data.phone)
    repository.add_user(session, user)
    await _flush(session)
    password_hash = await asyncio.to_thread(hash_password, data.password)
    await repository.set_password(session, user.id, password_hash)
    await session.refresh(user)
    set_fields = {name: getattr(user, name) for name in _USER_PERSONAL_FIELDS}
    await audit.record(
        session,
        organization_id=None,
        actor_user_id=actor_user_id,
        action="user.created",
        target_table="app_user",
        target_id=user.id,
        after={"is_active": user.is_active, **audit.changed_fields({}, set_fields)},
    )
    return user


async def create_user(session: AsyncSession, identity: Identity, data: UserCreate) -> AppUser:
    _require_platform_admin(identity.is_platform_admin)
    return await _create_user(session, data, identity.user_id)


async def bootstrap_platform_admin(session: AsyncSession, data: UserCreate) -> AppUser:
    """Create the first platform admin. Operator-only: called from the bootstrap CLI."""
    await bind_request_context(session, RequestContext(None, None, is_platform_admin=True))
    user = await _create_user(session, data, actor_user_id=None)
    grant = PlatformRoleAssignment(user_id=user.id, role=PlatformRole.PLATFORM_ADMIN)
    repository.add_platform_role(session, grant)
    await session.flush()
    await audit.record(
        session,
        organization_id=None,
        actor_user_id=None,
        action="platform_role.granted",
        target_table="platform_role_assignment",
        target_id=grant.id,
        after={"user_id": user.id, "role": grant.role},
    )
    return user


# --- organizations


async def create_organization(
    session: AsyncSession, identity: Identity, data: OrganizationCreate
) -> Organization:
    _require_platform_admin(identity.is_platform_admin)
    organization = Organization(name=data.name, slug=data.slug)
    repository.add_organization(session, organization)
    await _flush(session)
    await session.refresh(organization)
    await audit.record(
        session,
        organization_id=None,  # the organization's own chain starts with its first change
        actor_user_id=identity.user_id,
        action="organization.created",
        target_table="organization",
        target_id=organization.id,
        after=audit.snapshot(organization, _ORG_FIELDS),
    )
    return organization


async def list_organizations(session: AsyncSession, identity: Identity) -> list[Organization]:
    if identity.is_platform_admin:
        return await repository.list_all_organizations(session)
    return await repository.list_organizations_of_user(session, identity.user_id)


async def get_organization(session: AsyncSession, access: OrgAccess) -> Organization:
    organization = await repository.get_organization(session, access.organization_id)
    if organization is None:
        raise NotFoundError("Organization not found.")
    return organization


async def update_organization(
    session: AsyncSession, access: OrgAccess, data: OrganizationUpdate
) -> Organization:
    _require_platform_admin(access.is_platform_admin)
    organization = await get_organization(session, access)
    before = audit.snapshot(organization, _ORG_FIELDS)
    if data.name is not None:
        organization.name = data.name
    if data.status is not None:
        organization.status = data.status
    await _flush(session)
    await session.refresh(organization)
    await _record_update(
        session, access, "organization", organization.id, before, organization, _ORG_FIELDS
    )
    return organization


# --- entities


async def _get_entity(session: AsyncSession, access: OrgAccess, entity_id: UUID) -> Entity:
    entity = await repository.get_entity(session, access.organization_id, entity_id)
    if entity is None:
        raise NotFoundError("Entity not found.")
    return entity


async def _active_entity(
    session: AsyncSession, access: OrgAccess, entity_id: UUID, label: str = "Entity"
) -> Entity:
    entity = await repository.get_entity(session, access.organization_id, entity_id)
    if entity is None:
        raise NotFoundError(f"{label} not found.")
    if entity.archived_at is not None:
        raise ConflictError(f"{label} is archived.")
    return entity


async def create_entity(session: AsyncSession, access: OrgAccess, data: EntityCreate) -> Entity:
    _require_role(access, OrgRole.ORG_ADMIN)
    if data.parent_id is not None:
        await _active_entity(session, access, data.parent_id, "Parent entity")
    entity = Entity(
        organization_id=access.organization_id,
        parent_id=data.parent_id,
        code=data.code,
        name=data.name,
        kind=data.kind,
    )
    repository.add_entity(session, entity)
    await _flush(session)
    await session.refresh(entity)
    await _record_created(session, access, "entity", entity, _ENTITY_FIELDS)
    return entity


async def list_entities(
    session: AsyncSession, access: OrgAccess, *, include_archived: bool
) -> list[Entity]:
    return await repository.list_entities(
        session, access.organization_id, include_archived=include_archived
    )


async def get_entity(session: AsyncSession, access: OrgAccess, entity_id: UUID) -> Entity:
    return await _get_entity(session, access, entity_id)


async def update_entity(
    session: AsyncSession, access: OrgAccess, entity_id: UUID, data: EntityUpdate
) -> Entity:
    _require_role(access, OrgRole.ORG_ADMIN)
    entity = await _get_entity(session, access, entity_id)
    before = audit.snapshot(entity, _ENTITY_FIELDS)
    sent = data.model_fields_set

    if data.code is not None:
        entity.code = data.code
    if data.name is not None:
        entity.name = data.name
    if data.kind is not None:
        entity.kind = data.kind
    if "parent_id" in sent and data.parent_id != entity.parent_id:
        if data.parent_id is not None:
            await _active_entity(session, access, data.parent_id, "Parent entity")
        entity.parent_id = data.parent_id
    if data.archived is True and entity.archived_at is None:
        if await repository.has_active_children(session, access.organization_id, entity.id):
            raise ConflictError("Archive or move the entity's active children first.")
        entity.archived_at = datetime.now(UTC)
    elif data.archived is False and entity.archived_at is not None:
        if entity.parent_id is not None:
            await _active_entity(session, access, entity.parent_id, "Parent entity")
        entity.archived_at = None

    await _flush(session)
    await session.refresh(entity)
    await _record_update(session, access, "entity", entity.id, before, entity, _ENTITY_FIELDS)
    return entity


# --- reporting periods


async def _get_period(session: AsyncSession, access: OrgAccess, period_id: UUID) -> ReportingPeriod:
    period = await repository.get_period(session, access.organization_id, period_id)
    if period is None:
        raise NotFoundError("Reporting period not found.")
    return period


def _require_status(period: ReportingPeriod, status: PeriodStatus, action: str) -> None:
    if period.status != status:
        raise ConflictError(f"Only a {status} period can be {action}; this one is {period.status}.")


async def create_period(
    session: AsyncSession, access: OrgAccess, data: PeriodCreate
) -> ReportingPeriod:
    _require_role(access, OrgRole.ORG_ADMIN)
    period = ReportingPeriod(
        organization_id=access.organization_id,
        name=data.name,
        start_date=data.start_date,
        end_date=data.end_date,
    )
    repository.add_period(session, period)
    await _flush(session)
    await session.refresh(period)
    await _record_created(session, access, "reporting_period", period, _PERIOD_FIELDS)
    return period


async def list_periods(session: AsyncSession, access: OrgAccess) -> list[ReportingPeriod]:
    return await repository.list_periods(session, access.organization_id)


async def get_period(session: AsyncSession, access: OrgAccess, period_id: UUID) -> ReportingPeriod:
    return await _get_period(session, access, period_id)


async def update_period(
    session: AsyncSession, access: OrgAccess, period_id: UUID, data: PeriodUpdate
) -> ReportingPeriod:
    _require_role(access, OrgRole.ORG_ADMIN)
    period = await _get_period(session, access, period_id)
    _require_status(period, PeriodStatus.OPEN, "edited")
    before = audit.snapshot(period, _PERIOD_FIELDS)
    if data.name is not None:
        period.name = data.name
    if data.start_date is not None:
        period.start_date = data.start_date
    if data.end_date is not None:
        period.end_date = data.end_date
    if period.end_date < period.start_date:
        raise ConflictError("end_date must be on or after start_date.")
    await _flush(session)
    await session.refresh(period)
    await _record_update(
        session, access, "reporting_period", period.id, before, period, _PERIOD_FIELDS
    )
    return period


async def lock_period(session: AsyncSession, access: OrgAccess, period_id: UUID) -> ReportingPeriod:
    _require_role(access, OrgRole.ORG_ADMIN)
    period = await _get_period(session, access, period_id)
    _require_status(period, PeriodStatus.OPEN, "locked")
    before = audit.snapshot(period, _PERIOD_FIELDS)
    period.status = PeriodStatus.LOCKED
    period.locked_at = datetime.now(UTC)
    period.locked_by = access.user_id
    await _flush(session)
    await session.refresh(period)
    await _record_update(
        session,
        access,
        "reporting_period",
        period.id,
        before,
        period,
        _PERIOD_FIELDS,
        action="reporting_period.locked",
    )
    return period


async def unlock_period(
    session: AsyncSession, access: OrgAccess, period_id: UUID, reason: str
) -> ReportingPeriod:
    """Reopen a locked period. Platform admins and the organization's admins only."""
    _require_role(access, OrgRole.ORG_ADMIN)
    period = await _get_period(session, access, period_id)
    _require_status(period, PeriodStatus.LOCKED, "unlocked")
    before = audit.snapshot(period, _PERIOD_FIELDS)
    period.status = PeriodStatus.OPEN
    period.locked_at = None
    period.locked_by = None
    await _flush(session)
    await session.refresh(period)
    await _record_update(
        session,
        access,
        "reporting_period",
        period.id,
        before,
        period,
        _PERIOD_FIELDS,
        action="reporting_period.unlocked",
        reason=reason,
    )
    return period


async def publish_period(
    session: AsyncSession, access: OrgAccess, period_id: UUID
) -> ReportingPeriod:
    _require_role(access, OrgRole.ORG_ADMIN)
    period = await _get_period(session, access, period_id)
    _require_status(period, PeriodStatus.LOCKED, "published")
    before = audit.snapshot(period, _PERIOD_FIELDS)
    period.status = PeriodStatus.PUBLISHED
    period.published_at = datetime.now(UTC)
    period.published_by = access.user_id
    await _flush(session)
    await session.refresh(period)
    await _record_update(
        session,
        access,
        "reporting_period",
        period.id,
        before,
        period,
        _PERIOD_FIELDS,
        action="reporting_period.published",
    )
    return period


# --- role assignments


def _require_can_manage(access: OrgAccess, role: str) -> None:
    if role == OrgRole.AUDITOR:
        _require_platform_admin(access.is_platform_admin)
    else:
        _require_role(access, OrgRole.ORG_ADMIN)


def _assignment_out(assignment: RoleAssignment, user: AppUser) -> RoleAssignmentOut:
    return RoleAssignmentOut(
        id=assignment.id,
        user_id=assignment.user_id,
        user_email=user.email,
        user_display_name=user.display_name,
        role=OrgRole(assignment.role),
        entity_id=assignment.entity_id,
        granted_by=assignment.granted_by,
        granted_at=assignment.granted_at,
        revoked_at=assignment.revoked_at,
        revoked_by=assignment.revoked_by,
    )


async def grant_role(
    session: AsyncSession, access: OrgAccess, data: RoleAssignmentCreate
) -> RoleAssignmentOut:
    _require_can_manage(access, data.role)
    user_id = await repository.active_user_id_by_email(session, data.email)
    if user_id is None:
        raise NotFoundError("No active user with this email.")
    if data.entity_id is not None:
        await _active_entity(session, access, data.entity_id)
    if await repository.active_assignment_exists(
        session, access.organization_id, user_id, data.role, data.entity_id
    ):
        raise ConflictError(_CONSTRAINT_MESSAGES["uq_role_assignment_active"])
    assignment = RoleAssignment(
        organization_id=access.organization_id,
        user_id=user_id,
        role=data.role,
        entity_id=data.entity_id,
        granted_by=access.user_id,
    )
    repository.add_role_assignment(session, assignment)
    await _flush(session)
    await session.refresh(assignment)
    await audit.record(
        session,
        organization_id=access.organization_id,
        actor_user_id=access.user_id,
        action="role_assignment.granted",
        target_table="role_assignment",
        target_id=assignment.id,
        after={"user_id": user_id, "role": assignment.role, "entity_id": assignment.entity_id},
    )
    user = await repository.get_user(session, user_id)
    assert user is not None  # noqa: S101  (visible: now a member of the current organization)
    return _assignment_out(assignment, user)


async def list_role_assignments(
    session: AsyncSession, access: OrgAccess, *, include_revoked: bool
) -> list[RoleAssignmentOut]:
    _require_role(access, OrgRole.ORG_ADMIN, OrgRole.AUDITOR)
    rows = await repository.list_role_assignments(
        session, access.organization_id, include_revoked=include_revoked
    )
    return [_assignment_out(assignment, user) for assignment, user in rows]


async def revoke_role(
    session: AsyncSession, access: OrgAccess, assignment_id: UUID
) -> RoleAssignmentOut:
    assignment = await repository.get_role_assignment(
        session, access.organization_id, assignment_id
    )
    if assignment is None:
        raise NotFoundError("Role assignment not found.")
    _require_can_manage(access, assignment.role)
    if assignment.revoked_at is not None:
        raise ConflictError("This role assignment is already revoked.")
    assignment.revoked_at = datetime.now(UTC)
    assignment.revoked_by = access.user_id
    await _flush(session)
    await audit.record(
        session,
        organization_id=access.organization_id,
        actor_user_id=access.user_id,
        action="role_assignment.revoked",
        target_table="role_assignment",
        target_id=assignment.id,
        before={"user_id": assignment.user_id, "role": assignment.role, "revoked_at": None},
        after={"revoked_at": assignment.revoked_at},
    )
    user = await repository.get_user(session, assignment.user_id)
    assert user is not None  # noqa: S101  (members of the current organization are visible)
    return _assignment_out(assignment, user)
