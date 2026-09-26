"""catalog: data access. Every query must filter by organization_id.

Global rows have organization_id NULL. Tenant-facing queries return global rows plus the
current organization's rows (`_visible`); seeding queries touch global rows only (`_global`).
"""

from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.modules.catalog.models import (
    Dimension,
    DimensionValue,
    Disclosure,
    MetricDefinition,
    MetricDimension,
    Standard,
)


def _visible(column: InstrumentedAttribute[UUID | None], org: UUID) -> ColumnElement[bool]:
    return or_(column.is_(None), column == org)


def _global(column: InstrumentedAttribute[UUID | None]) -> ColumnElement[bool]:
    return column.is_(None)


def add(session: AsyncSession, row: object) -> None:
    session.add(row)


# --- standards and disclosures (global only)


async def list_standards(session: AsyncSession) -> list[Standard]:
    query = select(Standard).order_by(Standard.code, Standard.version)
    return list((await session.execute(query)).scalars())


async def get_standard(session: AsyncSession, standard_id: UUID) -> Standard | None:
    return await session.get(Standard, standard_id)


async def get_standard_by_key(session: AsyncSession, code: str, version: str) -> Standard | None:
    query = select(Standard).where(Standard.code == code, Standard.version == version)
    return (await session.execute(query)).scalar_one_or_none()


async def list_disclosures(session: AsyncSession, standard_id: UUID) -> list[Disclosure]:
    query = (
        select(Disclosure)
        .where(Disclosure.standard_id == standard_id)
        .order_by(Disclosure.sort_order, Disclosure.code)
    )
    return list((await session.execute(query)).scalars())


async def get_disclosure(session: AsyncSession, disclosure_id: UUID) -> Disclosure | None:
    return await session.get(Disclosure, disclosure_id)


async def get_disclosure_by_code(
    session: AsyncSession, standard_id: UUID, code: str
) -> Disclosure | None:
    query = select(Disclosure).where(Disclosure.standard_id == standard_id, Disclosure.code == code)
    return (await session.execute(query)).scalar_one_or_none()


# --- dimensions


async def get_global_dimension(session: AsyncSession, code: str) -> Dimension | None:
    query = select(Dimension).where(_global(Dimension.organization_id), Dimension.code == code)
    return (await session.execute(query)).scalar_one_or_none()


async def get_global_dimension_value(
    session: AsyncSession, dimension_id: UUID, code: str
) -> DimensionValue | None:
    query = select(DimensionValue).where(
        _global(DimensionValue.organization_id),
        DimensionValue.dimension_id == dimension_id,
        DimensionValue.code == code,
    )
    return (await session.execute(query)).scalar_one_or_none()


async def get_visible_dimension(
    session: AsyncSession, org: UUID, dimension_id: UUID
) -> Dimension | None:
    query = select(Dimension).where(
        _visible(Dimension.organization_id, org), Dimension.id == dimension_id
    )
    return (await session.execute(query)).scalar_one_or_none()


async def list_visible_dimensions(session: AsyncSession, org: UUID) -> list[Dimension]:
    query = (
        select(Dimension)
        .where(_visible(Dimension.organization_id, org))
        .order_by(Dimension.organization_id.is_not(None), Dimension.code)
    )
    return list((await session.execute(query)).scalars())


async def get_visible_dimension_value(
    session: AsyncSession, org: UUID, value_id: UUID
) -> DimensionValue | None:
    query = select(DimensionValue).where(
        _visible(DimensionValue.organization_id, org), DimensionValue.id == value_id
    )
    return (await session.execute(query)).scalar_one_or_none()


async def visible_values_by_dimension(
    session: AsyncSession, org: UUID, dimension_ids: Iterable[UUID], *, include_retired: bool
) -> dict[UUID, list[DimensionValue]]:
    query = (
        select(DimensionValue)
        .where(
            _visible(DimensionValue.organization_id, org),
            DimensionValue.dimension_id.in_(list(dimension_ids)),
        )
        .order_by(DimensionValue.sort_order, DimensionValue.code)
    )
    if not include_retired:
        query = query.where(DimensionValue.retired_at.is_(None))
    grouped: dict[UUID, list[DimensionValue]] = defaultdict(list)
    for value in (await session.execute(query)).scalars():
        grouped[value.dimension_id].append(value)
    return grouped


# --- metrics


async def get_global_metric(session: AsyncSession, code: str) -> MetricDefinition | None:
    query = select(MetricDefinition).where(
        _global(MetricDefinition.organization_id), MetricDefinition.code == code
    )
    return (await session.execute(query)).scalar_one_or_none()


async def get_visible_metric(
    session: AsyncSession, org: UUID, metric_id: UUID
) -> MetricDefinition | None:
    query = select(MetricDefinition).where(
        _visible(MetricDefinition.organization_id, org), MetricDefinition.id == metric_id
    )
    return (await session.execute(query)).scalar_one_or_none()


async def list_visible_metrics(
    session: AsyncSession, org: UUID, *, disclosure_id: UUID | None, include_retired: bool
) -> list[MetricDefinition]:
    query = (
        select(MetricDefinition)
        .join(Disclosure, Disclosure.id == MetricDefinition.disclosure_id)
        .where(_visible(MetricDefinition.organization_id, org))
        .order_by(
            Disclosure.code,
            MetricDefinition.organization_id.is_not(None),
            MetricDefinition.sort_order,
            MetricDefinition.code,
        )
    )
    if disclosure_id is not None:
        query = query.where(MetricDefinition.disclosure_id == disclosure_id)
    if not include_retired:
        query = query.where(MetricDefinition.retired_at.is_(None))
    return list((await session.execute(query)).scalars())


async def dimensions_by_metric(
    session: AsyncSession, metric_ids: Iterable[UUID]
) -> dict[UUID, list[tuple[MetricDimension, Dimension]]]:
    """Dimensions of the given metrics. Callers pass only metric ids they can see."""
    query = (
        select(MetricDimension, Dimension)
        .join(Dimension, Dimension.id == MetricDimension.dimension_id)
        .where(MetricDimension.metric_definition_id.in_(list(metric_ids)))
        .order_by(MetricDimension.sort_order, Dimension.code)
    )
    grouped: dict[UUID, list[tuple[MetricDimension, Dimension]]] = defaultdict(list)
    for link, dimension in (await session.execute(query)).all():
        grouped[link.metric_definition_id].append((link, dimension))
    return grouped
