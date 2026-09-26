"""tenancy: SQLAlchemy ORM models.

Row-level security policies, grants and triggers for these tables are in the migrations; the
ORM mirrors the columns and constraints so Alembic autogenerate stays accurate.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.auth import OrgRole
from app.core.db import Base, Timestamps, UUIDPrimaryKey


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class PlatformRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"


class EntityKind(StrEnum):
    GROUP = "group"
    LEGAL_ENTITY = "legal_entity"
    BUSINESS_UNIT = "business_unit"
    SITE = "site"


class PeriodStatus(StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    PUBLISHED = "published"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


_NO_ENTITY_SCOPE_ROLES = (OrgRole.ORG_ADMIN, OrgRole.AUDITOR)
_REVOKE_PAIR = "(revoked_at IS NULL) = (revoked_by IS NULL)"


class Organization(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint(_in("status", OrganizationStatus), name="status_valid"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_format"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    )

    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=OrganizationStatus.ACTIVE.value)


class AppUser(UUIDPrimaryKey, Timestamps, Base):
    """A person. Global: one login can hold roles in several organizations.

    Personal data is limited to email, display name and phone.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(r"email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'", name="email_format"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{6,14}$'", name="phone_e164"),
        CheckConstraint("btrim(display_name) <> ''", name="display_name_not_blank"),
        Index("uq_app_user_email_lower", func.lower(text("email")), unique=True),
    )

    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))


class PasswordCredential(Base):
    """Local password. The app role has no privileges here; it goes through SQL functions."""

    __tablename__ = "password_credential"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(UUIDPrimaryKey, Base):
    """A login token. Only its SHA-256 digest is stored."""

    __tablename__ = "auth_session"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id"), index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformRoleAssignment(UUIDPrimaryKey, Base):
    __tablename__ = "platform_role_assignment"
    __table_args__ = (
        CheckConstraint(_in("role", PlatformRole), name="role_valid"),
        CheckConstraint(_REVOKE_PAIR, name="revoke_pair"),
        Index(
            "uq_platform_role_assignment_active",
            "user_id",
            "role",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id"))
    role: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))  # null: bootstrap
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))


class Entity(UUIDPrimaryKey, Timestamps, Base):
    """A node in an organization's structure (group, legal entity, business unit, site)."""

    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "code"),
        ForeignKeyConstraint(
            ["organization_id", "parent_id"], ["entity.organization_id", "entity.id"]
        ),
        CheckConstraint("parent_id <> id", name="not_own_parent"),
        CheckConstraint(_in("kind", EntityKind), name="kind_valid"),
        CheckConstraint("btrim(code) <> ''", name="code_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"))
    parent_id: Mapped[UUID | None]
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportingPeriod(UUIDPrimaryKey, Timestamps, Base):
    """A reporting period. Dates are inclusive. Periods of one organization never overlap."""

    __tablename__ = "reporting_period"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name"),
        CheckConstraint("end_date >= start_date", name="dates_ordered"),
        CheckConstraint(_in("status", PeriodStatus), name="status_valid"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint(
            "(status = 'open' AND locked_at IS NULL AND locked_by IS NULL"
            " AND published_at IS NULL AND published_by IS NULL)"
            " OR (status = 'locked' AND locked_at IS NOT NULL AND locked_by IS NOT NULL"
            " AND published_at IS NULL AND published_by IS NULL)"
            " OR (status = 'published' AND locked_at IS NOT NULL AND locked_by IS NOT NULL"
            " AND published_at IS NOT NULL AND published_by IS NOT NULL)",
            name="status_fields_consistent",
        ),
        ExcludeConstraint(
            ("organization_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            using="gist",
            name="ex_reporting_period_no_overlap",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"))
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=PeriodStatus.OPEN.value)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))


class RoleAssignment(UUIDPrimaryKey, Base):
    """A user's role in one organization, optionally limited to an entity subtree.

    Never deleted: revoking sets revoked_at, so the history of who could do what is kept.
    """

    __tablename__ = "role_assignment"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "entity_id"], ["entity.organization_id", "entity.id"]
        ),
        CheckConstraint(_in("role", OrgRole), name="role_valid"),
        CheckConstraint(
            "entity_id IS NULL OR role NOT IN "
            f"({', '.join(repr(r.value) for r in _NO_ENTITY_SCOPE_ROLES)})",
            name="org_wide_roles_unscoped",
        ),
        CheckConstraint(_REVOKE_PAIR, name="revoke_pair"),
        Index(
            "uq_role_assignment_active",
            "organization_id",
            "user_id",
            "role",
            "entity_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_role_assignment_user_id", "user_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id"))
    role: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[UUID | None]
    granted_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))
