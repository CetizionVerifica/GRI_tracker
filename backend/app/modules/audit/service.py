"""audit: business logic and the module's public interface for other modules.

Other modules call `record` in the same transaction as the change it describes, so the change and
its audit entry are committed or rolled back together. The database assigns the entry's place in
its hash chain; nothing here can edit or delete an entry.

Personal data: callers pass field names, never values, for personal fields (email, phone, display
name), and never credentials. See `changed_fields`.
"""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import structlog
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Identity, OrgAccess, OrgRole
from app.core.errors import PermissionDeniedError
from app.modules.audit import repository
from app.modules.audit.models import ActorType, AuditLog
from app.modules.audit.schemas import AuditEntryOut, AuditPage, AuditQuery, ChainVerification

_READERS = (OrgRole.ORG_ADMIN, OrgRole.AUDITOR)


def _json(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """JSON-safe copy: Decimal becomes an exact string, UUIDs and dates become strings."""
    if values is None:
        return None
    converted: dict[str, Any] = to_jsonable_python(dict(values))
    return converted


def snapshot(obj: object, fields: Iterable[str]) -> dict[str, Any]:
    """The named attributes of `obj`, for before/after."""
    return {name: getattr(obj, name) for name in fields}


def diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only the keys whose values changed, as (before, after)."""
    changed = [key for key in after if before.get(key) != after[key]]
    return {k: before.get(k) for k in changed}, {k: after[k] for k in changed}


def changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """For personal data: which fields changed, without their values."""
    return {"changed_fields": sorted(k for k in after if before.get(k) != after[k])}


async def record(
    session: AsyncSession,
    *,
    organization_id: UUID | None,
    actor_user_id: UUID | None,
    action: str,
    target_table: str,
    target_id: UUID | None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """Append an entry. organization_id None records on the platform chain.

    actor_user_id None means the system acted (bootstrap, catalog seeding).
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await repository.insert_entry(
        session,
        {
            "organization_id": organization_id,
            "actor_user_id": actor_user_id,
            "actor_type": ActorType.USER if actor_user_id else ActorType.SYSTEM,
            "action": action,
            "target_table": target_table,
            "target_id": target_id,
            "before": _json(before),
            "after": _json(after),
            "reason": reason,
            "request_id": request_id if isinstance(request_id, str) else None,
        },
    )


def _entry_out(entry: AuditLog) -> AuditEntryOut:
    return AuditEntryOut(
        seq=entry.seq,
        id=entry.id,
        occurred_at=entry.occurred_at,
        actor_type=entry.actor_type,
        actor_user_id=entry.actor_user_id,
        action=entry.action,
        target_table=entry.target_table,
        target_id=entry.target_id,
        before=entry.before,
        after=entry.after,
        reason=entry.reason,
        request_id=entry.request_id,
        prev_hash=entry.prev_hash.hex(),
        row_hash=entry.row_hash.hex(),
    )


async def _page(
    session: AsyncSession, organization_id: UUID | None, query: AuditQuery
) -> AuditPage:
    rows = await repository.list_entries(session, organization_id, query)
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    return AuditPage(
        items=[_entry_out(r) for r in rows], next_before_seq=rows[-1].seq if more else None
    )


async def _verify(session: AsyncSession, organization_id: UUID | None) -> ChainVerification:
    rows_checked, first_broken = await repository.verify_chain(session, organization_id)
    return ChainVerification(
        organization_id=organization_id,
        rows_checked=rows_checked,
        intact=first_broken is None,
        first_broken_seq=first_broken,
    )


def _require_reader(access: OrgAccess) -> None:
    if not access.has_any_role(*_READERS):
        raise PermissionDeniedError("Only org admins, auditors and platform admins can do this.")


def _require_platform_admin(identity: Identity) -> None:
    if not identity.is_platform_admin:
        raise PermissionDeniedError("Only a platform admin can read the platform audit log.")


async def list_org_log(session: AsyncSession, access: OrgAccess, query: AuditQuery) -> AuditPage:
    _require_reader(access)
    return await _page(session, access.organization_id, query)


async def verify_org_chain(session: AsyncSession, access: OrgAccess) -> ChainVerification:
    _require_reader(access)
    return await _verify(session, access.organization_id)


async def list_platform_log(
    session: AsyncSession, identity: Identity, query: AuditQuery
) -> AuditPage:
    _require_platform_admin(identity)
    return await _page(session, None, query)


async def verify_platform_chain(session: AsyncSession, identity: Identity) -> ChainVerification:
    _require_platform_admin(identity)
    return await _verify(session, None)
