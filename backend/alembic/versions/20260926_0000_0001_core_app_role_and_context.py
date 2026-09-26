"""core: application role and request-context functions

Revision ID: 0001
Revises:
Create Date: 2026-09-26 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The app connects as a LOGIN role that is a member of gri_app. That login role and its
    # password are created by infrastructure, never by a migration. gri_app gets privileges
    # table by table in later migrations and never gets BYPASSRLS.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gri_app') THEN
                CREATE ROLE gri_app NOLOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO gri_app")

    # Read the transaction-local settings from app.core.db.bind_request_context. A missing or
    # empty setting gives NULL (or false), so tenant policies match no rows: they fail closed.
    op.execute(
        """
        CREATE FUNCTION app_current_org_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT nullif(current_setting('app.current_org_id', true), '')::uuid $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT nullif(current_setting('app.current_user_id', true), '')::uuid $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_is_platform_admin() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT coalesce(nullif(current_setting('app.is_platform_admin', true), '')::boolean,
                            false)
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app_is_platform_admin()")
    op.execute("DROP FUNCTION app_current_user_id()")
    op.execute("DROP FUNCTION app_current_org_id()")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gri_app")
    # Roles belong to the whole cluster and may have members created by infrastructure,
    # so gri_app is left in place.
