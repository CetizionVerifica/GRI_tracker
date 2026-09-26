"""audit: Pydantic request/response schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AuditQuery(BaseModel):
    """Filters for reading a chain, newest first. Page with before_seq = next_before_seq."""

    model_config = ConfigDict(extra="forbid")

    before_seq: int | None = Field(default=None, ge=1)
    limit: int = Field(default=100, ge=1, le=500)
    action: str | None = Field(default=None, max_length=200)
    target_table: str | None = Field(default=None, max_length=100)
    target_id: UUID | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None

    @model_validator(mode="after")
    def _range_ordered(self) -> "AuditQuery":
        if self.occurred_from and self.occurred_to and self.occurred_to < self.occurred_from:
            raise ValueError("occurred_to must not be before occurred_from")
        return self


class AuditEntryOut(BaseModel):
    seq: int
    id: UUID
    occurred_at: datetime
    actor_type: str
    actor_user_id: UUID | None
    action: str
    target_table: str
    target_id: UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str | None
    request_id: str | None
    prev_hash: str  # hex
    row_hash: str  # hex


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    next_before_seq: int | None


class ChainVerification(BaseModel):
    organization_id: UUID | None  # None: the platform chain
    rows_checked: int
    intact: bool
    first_broken_seq: int | None
