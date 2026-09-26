"""audit: SQLAlchemy ORM models.

The audit log is append-only and hash-chained. There is one chain per organization plus one
platform chain (organization_id NULL). A BEFORE INSERT trigger sets seq, occurred_at, prev_hash
and row_hash, so the application can neither backdate a row nor forge its place in the chain.
Other triggers reject UPDATE, DELETE and TRUNCATE. See migration 0004.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKey


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"


class AuditLog(UUIDPrimaryKey, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'system')", name="actor_type_valid"),
        CheckConstraint(
            "(actor_type = 'system') = (actor_user_id IS NULL)", name="actor_matches_type"
        ),
        CheckConstraint("btrim(action) <> ''", name="action_not_blank"),
        Index(
            "uq_audit_log_chain_seq",
            "organization_id",
            "seq",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_audit_log_target", "organization_id", "target_table", "target_id"),
    )

    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organization.id"))
    seq: Mapped[int] = mapped_column(BigInteger, server_default=FetchedValue())
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=FetchedValue()
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"))
    actor_type: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_table: Mapped[str] = mapped_column(Text)
    target_id: Mapped[UUID | None]
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, server_default=FetchedValue())
    row_hash: Mapped[bytes] = mapped_column(LargeBinary, server_default=FetchedValue())
