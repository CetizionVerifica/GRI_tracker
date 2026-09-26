"""audit: data access. Every query must filter by organization_id.

organization_id NULL selects the platform chain. Inserts use a plain INSERT without RETURNING:
the chain trigger fills in seq and the hashes, and the inserting user may not be allowed to read
the row back (a login event on the platform chain, for instance).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.audit.schemas import AuditQuery


async def insert_entry(session: AsyncSession, values: dict[str, Any]) -> None:
    await session.execute(insert(AuditLog).values(**values))


def _chain(organization_id: UUID | None) -> Any:
    if organization_id is None:
        return AuditLog.organization_id.is_(None)
    return AuditLog.organization_id == organization_id


async def list_entries(
    session: AsyncSession, organization_id: UUID | None, query: AuditQuery
) -> list[AuditLog]:
    statement = select(AuditLog).where(_chain(organization_id))
    if query.before_seq is not None:
        statement = statement.where(AuditLog.seq < query.before_seq)
    if query.action is not None:
        statement = statement.where(AuditLog.action == query.action)
    if query.target_table is not None:
        statement = statement.where(AuditLog.target_table == query.target_table)
    if query.target_id is not None:
        statement = statement.where(AuditLog.target_id == query.target_id)
    if query.occurred_from is not None:
        statement = statement.where(AuditLog.occurred_at >= query.occurred_from)
    if query.occurred_to is not None:
        statement = statement.where(AuditLog.occurred_at <= query.occurred_to)
    statement = statement.order_by(AuditLog.seq.desc()).limit(query.limit + 1)
    return list((await session.execute(statement)).scalars())


async def verify_chain(
    session: AsyncSession, organization_id: UUID | None
) -> tuple[int, int | None]:
    row = (
        await session.execute(
            text("SELECT rows_checked, first_broken_seq FROM audit_verify_chain(:org)"),
            {"org": organization_id},
        )
    ).one()
    return int(row.rows_checked), row.first_broken_seq
