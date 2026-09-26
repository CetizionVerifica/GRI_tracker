"""catalog: SQLAlchemy ORM models.

Standards and disclosures are global: seeded from backend/catalog/*.yaml, read-only to the app.
Dimensions, dimension values and metric definitions are global (organization_id NULL, seeded)
or belong to one organization (created through the API). Tenant codes start with "custom." so
they never collide with global codes. Row-level security, grants and scope triggers are in the
migrations.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey

TENANT_CODE_PREFIX = "custom."


class DataType(StrEnum):
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    TEXT = "text"
    CHOICE = "choice"
    MULTI_CHOICE = "multi_choice"  # several of the allowed choices, e.g. "gases included"
    DATE = "date"


NUMERIC_TYPES = (DataType.DECIMAL, DataType.INTEGER)


class Requirement(StrEnum):
    REQUIRED = "required"
    REQUIRED_IF_APPLICABLE = "required_if_applicable"
    OPTIONAL = "optional"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


# Global rows never use the tenant prefix; tenant rows always do.
_CODE_SCOPE = f"(organization_id IS NULL) = NOT starts_with(code, '{TENANT_CODE_PREFIX}')"


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Standard(UUIDPrimaryKey, Base):
    """A GRI Standard at one version, e.g. GRI 305 (2016). A new version means new rows.

    effective_date and effective_until are as GRI states them. GRI ties them to when a report
    is published, not to the reporting period.
    """

    __tablename__ = "standard"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_date", name="dates_ordered"
        ),
    )

    code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    effective_date: Mapped[date] = mapped_column(Date)
    effective_until: Mapped[date | None] = mapped_column(Date)  # last day it may be used
    created_at: Mapped[datetime] = _created_at()


class Disclosure(UUIDPrimaryKey, Base):
    """A disclosure. It can stop being in effect before its standard does (effective_until)."""

    __tablename__ = "disclosure"
    __table_args__ = (UniqueConstraint("standard_id", "code"),)

    standard_id: Mapped[UUID] = mapped_column(ForeignKey("standard.id"))
    code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    effective_until: Mapped[date | None] = mapped_column(Date)  # last day it may be used
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()


class Dimension(UUIDPrimaryKey, Base):
    """A way to break a metric down, e.g. greenhouse gas or site."""

    __tablename__ = "dimension"
    __table_args__ = (
        CheckConstraint(_CODE_SCOPE, name="code_scope"),
        Index(
            "uq_dimension_global_code",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_dimension_tenant_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organization.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DimensionValue(UUIDPrimaryKey, Base):
    """A value of a dimension. Tenants may add values to global dimensions (e.g. their sites)."""

    __tablename__ = "dimension_value"
    __table_args__ = (
        CheckConstraint(_CODE_SCOPE, name="code_scope"),
        Index(
            "uq_dimension_value_global_code",
            "dimension_id",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_dimension_value_tenant_code",
            "dimension_id",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organization.id"))
    dimension_id: Mapped[UUID] = mapped_column(ForeignKey("dimension.id"))
    code: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class MetricDefinition(UUIDPrimaryKey, Timestamps, Base):
    """What to collect for a disclosure: type, unit, validation and whether it is required.

    data_type, unit, validation_rules, calculated and the dimensions never change after
    creation: collected data depends on them. To change one, retire the metric and create a new
    one. A calculated metric is produced by the calculation module from other metrics and is
    never entered by hand.
    """

    __tablename__ = "metric_definition"
    __table_args__ = (
        CheckConstraint(_CODE_SCOPE, name="code_scope"),
        CheckConstraint(_in("data_type", DataType), name="data_type_valid"),
        CheckConstraint(_in("requirement", Requirement), name="requirement_valid"),
        CheckConstraint(
            f"(data_type IN ({', '.join(repr(t.value) for t in NUMERIC_TYPES)}))"
            " = (unit IS NOT NULL)",
            name="unit_iff_numeric",
        ),
        CheckConstraint("jsonb_typeof(validation_rules) = 'object'", name="rules_object"),
        Index(
            "uq_metric_definition_global_code",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_metric_definition_tenant_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organization.id"))
    disclosure_id: Mapped[UUID] = mapped_column(ForeignKey("disclosure.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    requirement: Mapped[str] = mapped_column(Text)
    validation_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    calculated: Mapped[bool] = mapped_column(server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetricDimension(Base):
    """A dimension a metric is broken down by. Same scope as its metric."""

    __tablename__ = "metric_dimension"

    metric_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("metric_definition.id"), primary_key=True
    )
    dimension_id: Mapped[UUID] = mapped_column(ForeignKey("dimension.id"), primary_key=True)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organization.id"))
    is_required: Mapped[bool] = mapped_column(server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
