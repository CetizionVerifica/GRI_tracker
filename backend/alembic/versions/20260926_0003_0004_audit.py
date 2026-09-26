"""audit: append-only, hash-chained audit log

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26 00:03:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Chains: one per organization, plus the platform chain (organization_id NULL).
# row_hash = sha256(prev_hash || payload), where payload is every field of the row joined with the
# ASCII unit separator. jsonb::text is canonical in Postgres (normalized key order and spacing), and
# occurred_at is hashed as exact epoch seconds, so the payload is reproducible byte for byte.
GENESIS = "decode(repeat('00', 32), 'hex')"

SECURITY: tuple[str, ...] = (
    """
    CREATE FUNCTION audit_log_payload(a audit_log) RETURNS bytea
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
        SELECT convert_to(concat_ws(E'\\x1f',
            a.seq::text,
            a.id::text,
            coalesce(a.organization_id::text, ''),
            extract(epoch FROM a.occurred_at)::text,
            coalesce(a.actor_user_id::text, ''),
            a.actor_type,
            a.action,
            a.target_table,
            coalesce(a.target_id::text, ''),
            coalesce(a.before::text, ''),
            coalesce(a.after::text, ''),
            coalesce(a.reason, ''),
            coalesce(a.request_id, '')
        ), 'UTF8')
    $$
    """,
    # SECURITY DEFINER: it must see the previous row of the chain even when the inserting user
    # can't read that chain (e.g. a login event on the platform chain).
    f"""
    CREATE FUNCTION audit_log_chain() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    DECLARE
        last_seq bigint;
        last_hash bytea;
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtextextended('audit_chain:' || coalesce(NEW.organization_id::text, 'platform'), 0));
        IF NEW.organization_id IS NULL THEN
            SELECT seq, row_hash INTO last_seq, last_hash FROM audit_log
            WHERE organization_id IS NULL ORDER BY seq DESC LIMIT 1;
        ELSE
            SELECT seq, row_hash INTO last_seq, last_hash FROM audit_log
            WHERE organization_id = NEW.organization_id ORDER BY seq DESC LIMIT 1;
        END IF;
        NEW.seq := coalesce(last_seq, 0) + 1;
        NEW.prev_hash := coalesce(last_hash, {GENESIS});
        NEW.occurred_at := clock_timestamp();
        NEW.row_hash := sha256(NEW.prev_hash || audit_log_payload(NEW));
        RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log"
    " FOR EACH ROW EXECUTE FUNCTION audit_log_chain()",
    """
    CREATE FUNCTION audit_log_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
    "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log"
    " FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change()",
    "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log"
    " FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()",
    # Row-level security. ENABLE without FORCE: the catalog seeder runs as the owner and records
    # platform events. The app role reads its organization's chain, or the platform chain as a
    # platform admin. It inserts only as itself (or as "system" when a platform admin), into
    # its current organization's chain, or into the platform chain for its own account events.
    "ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY audit_log_select ON audit_log FOR SELECT USING ("
    " organization_id = app_current_org_id()"
    " OR (organization_id IS NULL AND app_is_platform_admin()))",
    "CREATE POLICY audit_log_insert ON audit_log FOR INSERT WITH CHECK ("
    " ((actor_type = 'user' AND actor_user_id = app_current_user_id())"
    "  OR (actor_type = 'system' AND app_is_platform_admin()))"
    " AND (organization_id = app_current_org_id()"
    "  OR (organization_id IS NULL"
    "      AND (app_is_platform_admin() OR actor_user_id = app_current_user_id()))))",
    "GRANT SELECT, INSERT ON audit_log TO gri_app",
    # Recompute a chain. Runs as the caller, so it only sees (and verifies) chains the caller may
    # read. Returns the number of rows checked and the first seq that fails (NULL if intact).
    f"""
    CREATE FUNCTION audit_verify_chain(p_organization_id uuid)
    RETURNS TABLE (rows_checked bigint, first_broken_seq bigint)
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS $$
    DECLARE
        r audit_log;
        expected_prev bytea := {GENESIS};
        expected_seq bigint := 1;
    BEGIN
        rows_checked := 0;
        first_broken_seq := NULL;
        FOR r IN
            SELECT * FROM audit_log
            WHERE organization_id IS NOT DISTINCT FROM p_organization_id
            ORDER BY seq
        LOOP
            rows_checked := rows_checked + 1;
            IF r.seq <> expected_seq
               OR r.prev_hash <> expected_prev
               OR r.row_hash <> sha256(r.prev_hash || audit_log_payload(r)) THEN
                first_broken_seq := r.seq;
                RETURN NEXT;
                RETURN;
            END IF;
            expected_prev := r.row_hash;
            expected_seq := r.seq + 1;
        END LOOP;
        RETURN NEXT;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION audit_log_chain() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION audit_verify_chain(uuid) TO gri_app",
)


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('audit_log',
    sa.Column('organization_id', sa.Uuid(), nullable=True),
    sa.Column('seq', sa.BigInteger(), server_default=sa.FetchedValue(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.FetchedValue(), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('target_table', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=True),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('prev_hash', sa.LargeBinary(), server_default=sa.FetchedValue(), nullable=False),
    sa.Column('row_hash', sa.LargeBinary(), server_default=sa.FetchedValue(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("(actor_type = 'system') = (actor_user_id IS NULL)", name=op.f('ck_audit_log_actor_matches_type')),
    sa.CheckConstraint("actor_type IN ('user', 'system')", name=op.f('ck_audit_log_actor_type_valid')),
    sa.CheckConstraint("btrim(action) <> ''", name=op.f('ck_audit_log_action_not_blank')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['app_user.id'], name=op.f('fk_audit_log_actor_user_id_app_user')),
    sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_audit_log_organization_id_organization')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index('ix_audit_log_target', 'audit_log', ['organization_id', 'target_table', 'target_id'], unique=False)
    op.create_index('uq_audit_log_chain_seq', 'audit_log', ['organization_id', 'seq'], unique=True, postgresql_nulls_not_distinct=True)
    # ### end Alembic commands ###

    for statement in SECURITY:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP FUNCTION audit_verify_chain(uuid)")
    op.execute("DROP FUNCTION audit_log_payload(audit_log)")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('uq_audit_log_chain_seq', table_name='audit_log', postgresql_nulls_not_distinct=True)
    op.drop_index('ix_audit_log_target', table_name='audit_log')
    op.drop_table('audit_log')
    # ### end Alembic commands ###
    op.execute("DROP FUNCTION audit_log_reject_change()")
    op.execute("DROP FUNCTION audit_log_chain()")
