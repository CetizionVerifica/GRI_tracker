"""tenancy: Pydantic request/response schemas."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.auth import OrgRole
from app.modules.tenancy.models import EntityKind, OrganizationStatus, PeriodStatus

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320
    ),
]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+[1-9][0-9]{6,14}$")]
# Never stripped or transformed. Length only; no composition rules (NIST SP 800-63B).
NewPassword = Annotated[str, Field(min_length=12, max_length=128)]
AnyPassword = Annotated[str, Field(min_length=1, max_length=1024)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth


class LoginRequest(Input):
    email: Email
    password: AnyPassword


class LoginResponse(Output):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_at: datetime


class PasswordChange(Input):
    current_password: AnyPassword
    new_password: NewPassword


class Membership(Output):
    organization_id: UUID
    organization_name: str
    role: OrgRole
    entity_id: UUID | None


class Me(Output):
    id: UUID
    email: str
    display_name: str
    phone: str | None
    is_platform_admin: bool
    memberships: list[Membership]


# --- organizations and users


class OrganizationCreate(Input):
    name: Name
    slug: Slug


class OrganizationUpdate(Input):
    name: Name | None = None
    status: OrganizationStatus | None = None


class OrganizationOut(Output):
    id: UUID
    name: str
    slug: str
    status: OrganizationStatus
    created_at: datetime
    updated_at: datetime


class UserCreate(Input):
    email: Email
    display_name: Name
    phone: Phone | None = None
    password: NewPassword


class UserOut(Output):
    id: UUID
    email: str
    display_name: str
    phone: str | None
    is_active: bool
    created_at: datetime


# --- entities


class EntityCreate(Input):
    code: Code
    name: Name
    kind: EntityKind
    parent_id: UUID | None = None


class EntityUpdate(Input):
    """Only fields that are sent change. Send parent_id: null to make the entity a root."""

    code: Code | None = None
    name: Name | None = None
    kind: EntityKind | None = None
    parent_id: UUID | None = None
    archived: bool | None = None


class EntityOut(Output):
    id: UUID
    parent_id: UUID | None
    code: str
    name: str
    kind: EntityKind
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- reporting periods


class PeriodCreate(Input):
    name: Name
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _dates_ordered(self) -> "PeriodCreate":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class PeriodUpdate(Input):
    name: Name | None = None
    start_date: date | None = None
    end_date: date | None = None


class PeriodUnlock(Input):
    reason: Reason


class PeriodOut(Output):
    id: UUID
    name: str
    start_date: date
    end_date: date
    status: PeriodStatus
    locked_at: datetime | None
    locked_by: UUID | None
    published_at: datetime | None
    published_by: UUID | None
    created_at: datetime
    updated_at: datetime


# --- role assignments


class RoleAssignmentCreate(Input):
    email: Email
    role: OrgRole
    entity_id: UUID | None = None

    @model_validator(mode="after")
    def _org_wide_roles_unscoped(self) -> "RoleAssignmentCreate":
        if self.entity_id is not None and self.role in (OrgRole.ORG_ADMIN, OrgRole.AUDITOR):
            raise ValueError(f"{self.role} applies to the whole organization; omit entity_id")
        return self


class RoleAssignmentOut(Output):
    id: UUID
    user_id: UUID
    user_email: str
    user_display_name: str
    role: OrgRole
    entity_id: UUID | None
    granted_by: UUID
    granted_at: datetime
    revoked_at: datetime | None
    revoked_by: UUID | None
