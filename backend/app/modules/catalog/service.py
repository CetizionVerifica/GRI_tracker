"""catalog: business logic and the module's public interface for other modules.

Two parts:
- Seeding: load backend/catalog/*.yaml and upsert the global catalog. Runs as the owner. It
  only adds rows and updates descriptive fields; a change to anything collected data depends
  on (type, unit, rules, dimensions, disclosure) is an error.
- Tenant API: read the global catalog plus the organization's own metrics and dimension
  values, and let org admins add their own.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import OrgAccess
from app.core.db import flush_or_conflict
from app.core.errors import NotFoundError, PermissionDeniedError
from app.modules.catalog import repository
from app.modules.catalog.models import (
    DataType,
    Dimension,
    DimensionValue,
    Disclosure,
    MetricDefinition,
    MetricDimension,
    Requirement,
    Standard,
)
from app.modules.catalog.schemas import (
    CatalogFile,
    DimensionCreate,
    DimensionOut,
    DimensionValueCreate,
    DimensionValueOut,
    DimensionValueUpdate,
    DisclosureOut,
    MetricCreate,
    MetricDimensionOut,
    MetricOut,
    MetricUpdate,
    SeedDimension,
    SeedDisclosure,
    SeedMetric,
    SeedStandard,
    StandardDetail,
    StandardOut,
)
from app.modules.tenancy.service import OrgRole

_CONSTRAINT_MESSAGES = {
    "uq_dimension_tenant_code": "A dimension with this code already exists.",
    "uq_dimension_value_tenant_code": "This dimension already has a value with this code.",
    "uq_metric_definition_tenant_code": "A metric with this code already exists.",
}
_READ_ONLY = "Global catalog entries are read-only."


# --- seeding


class CatalogSeedError(Exception):
    """The seed files are invalid or conflict with what is already in the database."""


@dataclass
class SeedReport:
    created: Counter[str] = field(default_factory=Counter)
    updated: Counter[str] = field(default_factory=Counter)

    def __str__(self) -> str:
        created = ", ".join(f"{n} {kind}" for kind, n in sorted(self.created.items())) or "none"
        updated = ", ".join(f"{n} {kind}" for kind, n in sorted(self.updated.items())) or "none"
        return f"created: {created}; updated: {updated}"


def load_catalog(directory: Path) -> list[CatalogFile]:
    """Parse and cross-check every *.yaml file in `directory`, before touching the database."""
    files: list[CatalogFile] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            files.append(CatalogFile.model_validate(raw))
        except (yaml.YAMLError, ValidationError) as exc:
            raise CatalogSeedError(f"{path.name}: {exc}") from exc

    def duplicates(keys: list[Any]) -> list[Any]:
        return sorted(k for k, n in Counter(keys).items() if n > 1)

    dimension_codes = [d.code for f in files for d in f.dimensions]
    standard_keys = [(f.standard.code, f.standard.version) for f in files if f.standard]
    metrics = [m for f in files for d in f.disclosures for m in d.metrics]
    problems = [
        *(f"dimension {c} is defined twice" for c in duplicates(dimension_codes)),
        *(f"standard {c} {v} is defined twice" for c, v in duplicates(standard_keys)),
        *(f"metric {c} is defined twice" for c in duplicates([m.code for m in metrics])),
        *(
            f"metric {m.code} uses unknown dimension {d.code}"
            for m in metrics
            for d in m.dimensions
            if d.code not in set(dimension_codes)
        ),
    ]
    if problems:
        raise CatalogSeedError("; ".join(problems))
    return files


def _retired_at(retired: bool, current: datetime | None) -> datetime | None:
    if not retired:
        return None
    return current or datetime.now(UTC)


async def apply_catalog(session: AsyncSession, files: list[CatalogFile]) -> SeedReport:
    """Upsert the global catalog. Must run as the owner (it writes global rows)."""
    report = SeedReport()
    dimensions: dict[str, Dimension] = {}
    for catalog_file in files:
        for seed_dimension in catalog_file.dimensions:
            dimensions[seed_dimension.code] = await _seed_dimension(session, seed_dimension, report)
    for catalog_file in files:
        if catalog_file.standard is not None:
            standard = await _seed_standard(session, catalog_file.standard, report)
            for position, seed_disclosure in enumerate(catalog_file.disclosures):
                await _seed_disclosure(
                    session, standard, seed_disclosure, position, dimensions, report
                )
    await session.flush()
    return report


async def _seed_dimension(
    session: AsyncSession, seed: SeedDimension, report: SeedReport
) -> Dimension:
    dimension = await repository.get_global_dimension(session, seed.code)
    if dimension is None:
        dimension = Dimension(organization_id=None, code=seed.code, name=seed.name)
        repository.add(session, dimension)
        await session.flush()
        report.created["dimensions"] += 1
    elif dimension.name != seed.name:
        dimension.name = seed.name
        report.updated["dimensions"] += 1

    for position, seed_value in enumerate(seed.values):
        value = await repository.get_global_dimension_value(session, dimension.id, seed_value.code)
        retired_at = _retired_at(seed_value.retired, value.retired_at if value else None)
        if value is None:
            repository.add(
                session,
                DimensionValue(
                    organization_id=None,
                    dimension_id=dimension.id,
                    code=seed_value.code,
                    label=seed_value.label,
                    sort_order=position,
                    retired_at=retired_at,
                ),
            )
            report.created["dimension values"] += 1
        elif (value.label, value.sort_order, value.retired_at) != (
            seed_value.label,
            position,
            retired_at,
        ):
            value.label, value.sort_order, value.retired_at = seed_value.label, position, retired_at
            report.updated["dimension values"] += 1
    return dimension


async def _seed_standard(session: AsyncSession, seed: SeedStandard, report: SeedReport) -> Standard:
    standard = await repository.get_standard_by_key(session, seed.code, seed.version)
    if standard is None:
        standard = Standard(
            code=seed.code,
            title=seed.title,
            version=seed.version,
            effective_date=seed.effective_date,
            effective_until=seed.effective_until,
        )
        repository.add(session, standard)
        await session.flush()
        report.created["standards"] += 1
        return standard
    if standard.effective_date != seed.effective_date:
        raise CatalogSeedError(
            f"standard {seed.code} {seed.version}: effective_date cannot change"
            f" ({standard.effective_date} -> {seed.effective_date}); add a new version instead"
        )
    if (standard.title, standard.effective_until) != (seed.title, seed.effective_until):
        standard.title, standard.effective_until = seed.title, seed.effective_until
        report.updated["standards"] += 1
    return standard


async def _seed_disclosure(
    session: AsyncSession,
    standard: Standard,
    seed: SeedDisclosure,
    position: int,
    dimensions: dict[str, Dimension],
    report: SeedReport,
) -> None:
    disclosure = await repository.get_disclosure_by_code(session, standard.id, seed.code)
    if disclosure is None:
        disclosure = Disclosure(
            standard_id=standard.id,
            code=seed.code,
            title=seed.title,
            effective_until=seed.effective_until,
            sort_order=position,
        )
        repository.add(session, disclosure)
        await session.flush()
        report.created["disclosures"] += 1
    elif (disclosure.title, disclosure.effective_until, disclosure.sort_order) != (
        seed.title,
        seed.effective_until,
        position,
    ):
        disclosure.title = seed.title
        disclosure.effective_until = seed.effective_until
        disclosure.sort_order = position
        report.updated["disclosures"] += 1

    for metric_position, seed_metric in enumerate(seed.metrics):
        await _seed_metric(session, disclosure, seed_metric, metric_position, dimensions, report)


async def _seed_metric(
    session: AsyncSession,
    disclosure: Disclosure,
    seed: SeedMetric,
    position: int,
    dimensions: dict[str, Dimension],
    report: SeedReport,
) -> None:
    rules = seed.validation.as_json()
    metric = await repository.get_global_metric(session, seed.code)
    if metric is None:
        metric = MetricDefinition(
            organization_id=None,
            disclosure_id=disclosure.id,
            code=seed.code,
            name=seed.name,
            description=seed.description,
            data_type=seed.data_type,
            unit=seed.unit,
            requirement=seed.requirement,
            validation_rules=rules,
            sort_order=position,
            retired_at=_retired_at(seed.retired, None),
        )
        repository.add(session, metric)
        await session.flush()
        for dim_position, link in enumerate(seed.dimensions):
            repository.add(
                session,
                MetricDimension(
                    metric_definition_id=metric.id,
                    dimension_id=dimensions[link.code].id,
                    organization_id=None,
                    is_required=link.required,
                    sort_order=dim_position,
                ),
            )
        report.created["metrics"] += 1
        return

    existing_links = (await repository.dimensions_by_metric(session, [metric.id]))[metric.id]
    fixed = {
        "disclosure": (metric.disclosure_id, disclosure.id),
        "data_type": (metric.data_type, seed.data_type),
        "unit": (metric.unit, seed.unit),
        "validation": (metric.validation_rules, rules),
        "dimensions": (
            [(d.code, link.is_required) for link, d in existing_links],
            [(d.code, d.required) for d in seed.dimensions],
        ),
    }
    if changed := [name for name, (old, new) in fixed.items() if old != new]:
        raise CatalogSeedError(
            f"metric {seed.code}: {', '.join(changed)} cannot change once seeded;"
            " retire it and add a metric with a new code"
        )
    descriptive = (seed.name, seed.description, seed.requirement, position)
    retired_at = _retired_at(seed.retired, metric.retired_at)
    if (
        metric.name,
        metric.description,
        metric.requirement,
        metric.sort_order,
    ) != descriptive or metric.retired_at != retired_at:
        metric.name, metric.description, metric.requirement, metric.sort_order = descriptive
        metric.retired_at = retired_at
        report.updated["metrics"] += 1


# --- tenant API: helpers


def _require_admin(access: OrgAccess) -> None:
    if not access.has_any_role(OrgRole.ORG_ADMIN):
        raise PermissionDeniedError("Your role does not allow this action.")


def _value_out(value: DimensionValue) -> DimensionValueOut:
    return DimensionValueOut(
        id=value.id,
        dimension_id=value.dimension_id,
        code=value.code,
        label=value.label,
        sort_order=value.sort_order,
        is_custom=value.organization_id is not None,
        retired_at=value.retired_at,
    )


def _dimension_out(dimension: Dimension, values: list[DimensionValue]) -> DimensionOut:
    return DimensionOut(
        id=dimension.id,
        code=dimension.code,
        name=dimension.name,
        is_custom=dimension.organization_id is not None,
        values=[_value_out(v) for v in values],
    )


async def _metrics_out(session: AsyncSession, metrics: list[MetricDefinition]) -> list[MetricOut]:
    links = await repository.dimensions_by_metric(session, [m.id for m in metrics])
    return [
        MetricOut(
            id=m.id,
            disclosure_id=m.disclosure_id,
            code=m.code,
            name=m.name,
            description=m.description,
            data_type=DataType(m.data_type),
            unit=m.unit,
            requirement=Requirement(m.requirement),
            validation_rules=m.validation_rules,
            is_custom=m.organization_id is not None,
            retired_at=m.retired_at,
            dimensions=[
                MetricDimensionOut(
                    dimension_id=d.id, code=d.code, name=d.name, is_required=link.is_required
                )
                for link, d in links.get(m.id, [])
            ],
        )
        for m in metrics
    ]


# --- tenant API: standards


async def list_standards(session: AsyncSession) -> list[StandardOut]:
    return [StandardOut.model_validate(s) for s in await repository.list_standards(session)]


async def get_standard(session: AsyncSession, standard_id: UUID) -> StandardDetail:
    standard = await repository.get_standard(session, standard_id)
    if standard is None:
        raise NotFoundError("Standard not found.")
    disclosures = await repository.list_disclosures(session, standard.id)
    return StandardDetail(
        **StandardOut.model_validate(standard).model_dump(),
        disclosures=[DisclosureOut.model_validate(d) for d in disclosures],
    )


# --- tenant API: dimensions


async def list_dimensions(
    session: AsyncSession, access: OrgAccess, *, include_retired: bool
) -> list[DimensionOut]:
    org = access.organization_id
    dimensions = await repository.list_visible_dimensions(session, org)
    values = await repository.visible_values_by_dimension(
        session, org, [d.id for d in dimensions], include_retired=include_retired
    )
    return [_dimension_out(d, values.get(d.id, [])) for d in dimensions]


async def create_dimension(
    session: AsyncSession, access: OrgAccess, data: DimensionCreate
) -> DimensionOut:
    _require_admin(access)
    dimension = Dimension(organization_id=access.organization_id, code=data.code, name=data.name)
    repository.add(session, dimension)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    values = [
        DimensionValue(
            organization_id=access.organization_id,
            dimension_id=dimension.id,
            code=v.code,
            label=v.label,
            sort_order=v.sort_order,
        )
        for v in data.values
    ]
    for value in values:
        repository.add(session, value)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    return _dimension_out(dimension, values)


async def add_dimension_value(
    session: AsyncSession, access: OrgAccess, dimension_id: UUID, data: DimensionValueCreate
) -> DimensionValueOut:
    """Add an organization's own value, to its own dimension or to a global one."""
    _require_admin(access)
    dimension = await repository.get_visible_dimension(
        session, access.organization_id, dimension_id
    )
    if dimension is None:
        raise NotFoundError("Dimension not found.")
    value = DimensionValue(
        organization_id=access.organization_id,
        dimension_id=dimension.id,
        code=data.code,
        label=data.label,
        sort_order=data.sort_order,
    )
    repository.add(session, value)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    return _value_out(value)


async def update_dimension_value(
    session: AsyncSession, access: OrgAccess, value_id: UUID, data: DimensionValueUpdate
) -> DimensionValueOut:
    _require_admin(access)
    value = await repository.get_visible_dimension_value(session, access.organization_id, value_id)
    if value is None:
        raise NotFoundError("Dimension value not found.")
    if value.organization_id is None:
        raise PermissionDeniedError(_READ_ONLY)
    if data.label is not None:
        value.label = data.label
    if data.sort_order is not None:
        value.sort_order = data.sort_order
    if data.retired is not None:
        value.retired_at = _retired_at(data.retired, value.retired_at)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    return _value_out(value)


# --- tenant API: metrics


async def list_metrics(
    session: AsyncSession,
    access: OrgAccess,
    *,
    disclosure_id: UUID | None,
    include_retired: bool,
) -> list[MetricOut]:
    metrics = await repository.list_visible_metrics(
        session,
        access.organization_id,
        disclosure_id=disclosure_id,
        include_retired=include_retired,
    )
    return await _metrics_out(session, metrics)


async def _get_metric(
    session: AsyncSession, access: OrgAccess, metric_id: UUID
) -> MetricDefinition:
    metric = await repository.get_visible_metric(session, access.organization_id, metric_id)
    if metric is None:
        raise NotFoundError("Metric not found.")
    return metric


async def get_metric(session: AsyncSession, access: OrgAccess, metric_id: UUID) -> MetricOut:
    return (await _metrics_out(session, [await _get_metric(session, access, metric_id)]))[0]


async def create_metric(session: AsyncSession, access: OrgAccess, data: MetricCreate) -> MetricOut:
    _require_admin(access)
    if await repository.get_disclosure(session, data.disclosure_id) is None:
        raise NotFoundError("Disclosure not found.")
    for link in data.dimensions:
        if (
            await repository.get_visible_dimension(
                session, access.organization_id, link.dimension_id
            )
            is None
        ):
            raise NotFoundError(f"Dimension {link.dimension_id} not found.")

    metric = MetricDefinition(
        organization_id=access.organization_id,
        disclosure_id=data.disclosure_id,
        code=data.code,
        name=data.name,
        description=data.description,
        data_type=data.data_type,
        unit=data.unit,
        requirement=data.requirement,
        validation_rules=data.validation_rules.as_json(),
    )
    repository.add(session, metric)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    for position, link in enumerate(data.dimensions):
        repository.add(
            session,
            MetricDimension(
                metric_definition_id=metric.id,
                dimension_id=link.dimension_id,
                organization_id=access.organization_id,
                is_required=link.is_required,
                sort_order=position,
            ),
        )
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    await session.refresh(metric)
    return (await _metrics_out(session, [metric]))[0]


async def update_metric(
    session: AsyncSession, access: OrgAccess, metric_id: UUID, data: MetricUpdate
) -> MetricOut:
    _require_admin(access)
    metric = await _get_metric(session, access, metric_id)
    if metric.organization_id is None:
        raise PermissionDeniedError(_READ_ONLY)
    if data.name is not None:
        metric.name = data.name
    if "description" in data.model_fields_set:
        metric.description = data.description
    if data.requirement is not None:
        metric.requirement = data.requirement
    if data.retired is not None:
        metric.retired_at = _retired_at(data.retired, metric.retired_at)
    await flush_or_conflict(session, _CONSTRAINT_MESSAGES)
    await session.refresh(metric)
    return (await _metrics_out(session, [metric]))[0]
