"""Database engine, session factory and the declarative base for all ORM models."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import Connection, DateTime, FetchedValue, MetaData, event, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, SessionTransaction, mapped_column

from app.core.ids import uuid7

# Deterministic constraint names so Alembic autogenerate produces stable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)


class Timestamps:
    """created_at and updated_at from the database clock; a trigger maintains updated_at."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=FetchedValue()
    )


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint behind an IntegrityError, for mapping it to a domain error."""
    diag = getattr(exc.orig, "diag", None)
    name: str | None = getattr(diag, "constraint_name", None)
    return name


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on success and rolls back on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transactional session per request."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async for session in session_scope(factory):
        yield session


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is acting, and in which organization. Row-level security policies read this."""

    organization_id: UUID | None
    user_id: UUID | None
    is_platform_admin: bool = False


_CONTEXT_KEY = "request_context"

# Transaction-local settings (is_local=true): they end with the transaction, so a pooled
# connection never carries one request's tenant into the next. The SQL functions
# app_current_org_id(), app_current_user_id() and app_is_platform_admin() read them.
_SET_CONTEXT_SQL = text(
    "SELECT set_config('app.current_org_id', :org, true),"
    " set_config('app.current_user_id', :user, true),"
    " set_config('app.is_platform_admin', :admin, true)"
)


def _context_params(ctx: RequestContext) -> dict[str, str]:
    return {
        "org": str(ctx.organization_id) if ctx.organization_id else "",
        "user": str(ctx.user_id) if ctx.user_id else "",
        "admin": "true" if ctx.is_platform_admin else "false",
    }


async def bind_request_context(session: AsyncSession, ctx: RequestContext) -> None:
    """Scope `session` to `ctx`, now and for every later transaction it begins."""
    session.info[_CONTEXT_KEY] = ctx
    if session.in_transaction():
        await session.execute(_SET_CONTEXT_SQL, _context_params(ctx))


@event.listens_for(Session, "after_begin")
def _apply_request_context(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    ctx: Any = session.info.get(_CONTEXT_KEY)
    if isinstance(ctx, RequestContext):
        connection.execute(_SET_CONTEXT_SQL, _context_params(ctx))


DbSession = Annotated[AsyncSession, Depends(get_session)]
