"""tenancy: data access. Every query must filter by organization_id.

Row-level security enforces the same boundary in the database; the explicit filters here keep
queries correct on their own and make intent obvious in review.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tenancy.models import (
    AppUser,
    AuthSession,
    Entity,
    Organization,
    PlatformRoleAssignment,
    ReportingPeriod,
    RoleAssignment,
)


@dataclass(frozen=True, slots=True)
class LoginRecord:
    user_id: UUID
    password_hash: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    user_id: UUID
    is_platform_admin: bool


# --- auth (security-definer functions; see migration 0002)


async def login_lookup(session: AsyncSession, email: str) -> LoginRecord | None:
    row = (
        await session.execute(
            text("SELECT user_id, password_hash, is_active FROM auth_login_lookup(:email)"),
            {"email": email},
        )
    ).first()
    return LoginRecord(row.user_id, row.password_hash, row.is_active) if row else None


async def resolve_session(session: AsyncSession, token_hash: bytes) -> ResolvedSession | None:
    row = (
        await session.execute(
            text("SELECT user_id, is_platform_admin FROM auth_resolve_session(:token_hash)"),
            {"token_hash": token_hash},
        )
    ).first()
    return ResolvedSession(row.user_id, row.is_platform_admin) if row else None


async def set_password(session: AsyncSession, user_id: UUID, password_hash: str) -> None:
    await session.execute(
        text("SELECT auth_set_password(:user_id, :password_hash)"),
        {"user_id": user_id, "password_hash": password_hash},
    )


async def active_user_id_by_email(session: AsyncSession, email: str) -> UUID | None:
    user_id: UUID | None = (
        await session.execute(text("SELECT app_user_id_by_email(:email)"), {"email": email})
    ).scalar_one()
    return user_id


def add_auth_session(
    session: AsyncSession, user_id: UUID, token_hash: bytes, expires_at: datetime
) -> None:
    session.add(AuthSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at))


async def get_auth_session(
    session: AsyncSession, user_id: UUID, token_hash: bytes
) -> AuthSession | None:
    return (
        await session.execute(
            select(AuthSession).where(
                AuthSession.user_id == user_id, AuthSession.token_hash == token_hash
            )
        )
    ).scalar_one_or_none()


# --- users


async def get_user(session: AsyncSession, user_id: UUID) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def email_taken(session: AsyncSession, email: str) -> bool:
    """Uses the security-definer lookup so inactive and invisible users count too."""
    return (await login_lookup(session, email)) is not None


def add_user(session: AsyncSession, user: AppUser) -> None:
    session.add(user)


def add_platform_role(session: AsyncSession, grant: PlatformRoleAssignment) -> None:
    session.add(grant)


# --- organizations


async def get_organization(session: AsyncSession, organization_id: UUID) -> Organization | None:
    return await session.get(Organization, organization_id)


async def list_all_organizations(session: AsyncSession) -> list[Organization]:
    return list((await session.execute(select(Organization).order_by(Organization.name))).scalars())


async def list_organizations_of_user(session: AsyncSession, user_id: UUID) -> list[Organization]:
    member = exists().where(
        RoleAssignment.organization_id == Organization.id,
        RoleAssignment.user_id == user_id,
        RoleAssignment.revoked_at.is_(None),
    )
    return list(
        (await session.execute(select(Organization).where(member).order_by(Organization.name)))
        .scalars()
        .all()
    )


def add_organization(session: AsyncSession, organization: Organization) -> None:
    session.add(organization)


# --- role assignments


async def active_roles(session: AsyncSession, organization_id: UUID, user_id: UUID) -> set[str]:
    rows = await session.execute(
        select(RoleAssignment.role).where(
            RoleAssignment.organization_id == organization_id,
            RoleAssignment.user_id == user_id,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    return set(rows.scalars())


async def memberships_of_user(
    session: AsyncSession, user_id: UUID
) -> list[tuple[RoleAssignment, str]]:
    rows = await session.execute(
        select(RoleAssignment, Organization.name)
        .join(Organization, Organization.id == RoleAssignment.organization_id)
        .where(RoleAssignment.user_id == user_id, RoleAssignment.revoked_at.is_(None))
        .order_by(Organization.name, RoleAssignment.role)
    )
    return [(assignment, name) for assignment, name in rows.all()]


async def get_role_assignment(
    session: AsyncSession, organization_id: UUID, assignment_id: UUID
) -> RoleAssignment | None:
    return (
        await session.execute(
            select(RoleAssignment).where(
                RoleAssignment.organization_id == organization_id,
                RoleAssignment.id == assignment_id,
            )
        )
    ).scalar_one_or_none()


async def list_role_assignments(
    session: AsyncSession, organization_id: UUID, *, include_revoked: bool
) -> list[tuple[RoleAssignment, AppUser]]:
    query = (
        select(RoleAssignment, AppUser)
        .join(AppUser, AppUser.id == RoleAssignment.user_id)
        .where(RoleAssignment.organization_id == organization_id)
        .order_by(AppUser.email, RoleAssignment.role, RoleAssignment.granted_at)
    )
    if not include_revoked:
        query = query.where(RoleAssignment.revoked_at.is_(None))
    return [(assignment, user) for assignment, user in (await session.execute(query)).all()]


async def active_assignment_exists(
    session: AsyncSession, organization_id: UUID, user_id: UUID, role: str, entity_id: UUID | None
) -> bool:
    query = select(RoleAssignment.id).where(
        RoleAssignment.organization_id == organization_id,
        RoleAssignment.user_id == user_id,
        RoleAssignment.role == role,
        RoleAssignment.entity_id.is_(None)
        if entity_id is None
        else RoleAssignment.entity_id == entity_id,
        RoleAssignment.revoked_at.is_(None),
    )
    return (await session.execute(query)).first() is not None


def add_role_assignment(session: AsyncSession, assignment: RoleAssignment) -> None:
    session.add(assignment)


# --- entities


async def get_entity(
    session: AsyncSession, organization_id: UUID, entity_id: UUID
) -> Entity | None:
    return (
        await session.execute(
            select(Entity).where(Entity.organization_id == organization_id, Entity.id == entity_id)
        )
    ).scalar_one_or_none()


async def list_entities(
    session: AsyncSession, organization_id: UUID, *, include_archived: bool
) -> list[Entity]:
    query = select(Entity).where(Entity.organization_id == organization_id).order_by(Entity.code)
    if not include_archived:
        query = query.where(Entity.archived_at.is_(None))
    return list((await session.execute(query)).scalars())


async def has_active_children(
    session: AsyncSession, organization_id: UUID, entity_id: UUID
) -> bool:
    query = select(Entity.id).where(
        Entity.organization_id == organization_id,
        Entity.parent_id == entity_id,
        Entity.archived_at.is_(None),
    )
    return (await session.execute(query.limit(1))).first() is not None


def add_entity(session: AsyncSession, entity: Entity) -> None:
    session.add(entity)


# --- reporting periods


async def get_period(
    session: AsyncSession, organization_id: UUID, period_id: UUID
) -> ReportingPeriod | None:
    return (
        await session.execute(
            select(ReportingPeriod).where(
                ReportingPeriod.organization_id == organization_id,
                ReportingPeriod.id == period_id,
            )
        )
    ).scalar_one_or_none()


async def list_periods(session: AsyncSession, organization_id: UUID) -> list[ReportingPeriod]:
    query = (
        select(ReportingPeriod)
        .where(ReportingPeriod.organization_id == organization_id)
        .order_by(ReportingPeriod.start_date)
    )
    return list((await session.execute(query)).scalars())


def add_period(session: AsyncSession, period: ReportingPeriod) -> None:
    session.add(period)
