"""tenancy: organizations, users, entities, periods, roles

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 00:01:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTH_FUNCTIONS = (
    "auth_login_lookup(text)",
    "auth_resolve_session(bytea)",
    "auth_set_password(uuid, text)",
    "app_user_id_by_email(text)",
)

SET_UPDATED_AT = """
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$
"""

_ORG_ADMIN_OF_NEW_ORG = """
    EXISTS (
        SELECT 1 FROM role_assignment ra
        WHERE ra.organization_id = NEW.organization_id
          AND ra.user_id = app_current_user_id()
          AND ra.role = 'org_admin'
          AND ra.revoked_at IS NULL
    )
"""

# Row-level security, grants, triggers and the security-definer auth functions.
# Policies are the backstop; the service checks the same rules first and gives clear errors.
SECURITY: tuple[str, ...] = (
    # updated_at
    *(
        f"CREATE TRIGGER {t}_set_updated_at BEFORE UPDATE ON {t}"
        " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        for t in ("organization", "app_user", "entity", "reporting_period")
    ),
    # --- organization: platform admins see all; others see the current org and their own orgs.
    "ALTER TABLE organization ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE organization FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY organization_select ON organization FOR SELECT USING (
        id = app_current_org_id()
        OR app_is_platform_admin()
        OR EXISTS (
            SELECT 1 FROM role_assignment ra
            WHERE ra.organization_id = organization.id
              AND ra.user_id = app_current_user_id()
              AND ra.revoked_at IS NULL
        )
    )
    """,
    "CREATE POLICY organization_insert ON organization FOR INSERT"
    " WITH CHECK (app_is_platform_admin())",
    "CREATE POLICY organization_update ON organization FOR UPDATE"
    " USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin())",
    "GRANT SELECT, INSERT, UPDATE (name, status) ON organization TO gri_app",
    # --- app_user: global. Visible to yourself, platform admins, and members of the current org.
    # ENABLE without FORCE: the owner-run security-definer auth functions below must see it.
    "ALTER TABLE app_user ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY app_user_select ON app_user FOR SELECT USING (
        id = app_current_user_id()
        OR app_is_platform_admin()
        OR EXISTS (
            SELECT 1 FROM role_assignment ra
            WHERE ra.user_id = app_user.id AND ra.organization_id = app_current_org_id()
        )
    )
    """,
    "CREATE POLICY app_user_insert ON app_user FOR INSERT WITH CHECK (app_is_platform_admin())",
    "CREATE POLICY app_user_update ON app_user FOR UPDATE"
    " USING (id = app_current_user_id() OR app_is_platform_admin())"
    " WITH CHECK (id = app_current_user_id() OR app_is_platform_admin())",
    "GRANT SELECT, INSERT, UPDATE (email, display_name, phone, is_active) ON app_user TO gri_app",
    # --- password_credential: no policies and no grants. Only reachable through the functions.
    "ALTER TABLE password_credential ENABLE ROW LEVEL SECURITY",
    # --- auth_session: your own sessions only.
    "ALTER TABLE auth_session ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY auth_session_own ON auth_session"
    " USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id())",
    "GRANT SELECT, INSERT, UPDATE (revoked_at) ON auth_session TO gri_app",
    # --- platform_role_assignment: only platform admins grant or revoke.
    "ALTER TABLE platform_role_assignment ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY platform_role_select ON platform_role_assignment FOR SELECT"
    " USING (user_id = app_current_user_id() OR app_is_platform_admin())",
    "CREATE POLICY platform_role_insert ON platform_role_assignment FOR INSERT"
    " WITH CHECK (app_is_platform_admin())",
    "CREATE POLICY platform_role_update ON platform_role_assignment FOR UPDATE"
    " USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin())",
    "GRANT SELECT, INSERT, UPDATE (revoked_at, revoked_by) ON platform_role_assignment TO gri_app",
    # --- entity: tenant table; organization_id is never updatable.
    "ALTER TABLE entity ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entity FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON entity"
    " USING (organization_id = app_current_org_id())"
    " WITH CHECK (organization_id = app_current_org_id())",
    "GRANT SELECT, INSERT, UPDATE (parent_id, code, name, kind, archived_at) ON entity TO gri_app",
    """
    CREATE FUNCTION entity_prevent_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.parent_id IS NULL THEN
            RETURN NEW;
        END IF;
        -- Serialize tree changes per organization so two concurrent moves can't form a cycle.
        PERFORM pg_advisory_xact_lock(
            hashtextextended('entity_tree:' || NEW.organization_id::text, 0));
        IF EXISTS (
            WITH RECURSIVE ancestors(id, parent_id) AS (
                SELECT id, parent_id FROM entity WHERE id = NEW.parent_id
                UNION
                SELECT e.id, e.parent_id FROM entity e JOIN ancestors a ON e.id = a.parent_id
            )
            SELECT 1 FROM ancestors WHERE id = NEW.id
        ) THEN
            RAISE EXCEPTION 'entity % cannot be moved under its own descendant', NEW.id
                USING ERRCODE = 'check_violation', CONSTRAINT = 'entity_no_cycle';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER entity_prevent_cycle BEFORE INSERT OR UPDATE OF parent_id ON entity"
    " FOR EACH ROW EXECUTE FUNCTION entity_prevent_cycle()",
    # --- reporting_period: tenant table with a status machine.
    "ALTER TABLE reporting_period ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE reporting_period FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON reporting_period"
    " USING (organization_id = app_current_org_id())"
    " WITH CHECK (organization_id = app_current_org_id())",
    "GRANT SELECT, INSERT, UPDATE (name, start_date, end_date, status, locked_at, locked_by,"
    " published_at, published_by) ON reporting_period TO gri_app",
    f"""
    CREATE FUNCTION reporting_period_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'open' THEN
                RAISE EXCEPTION 'a reporting period must be created open'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'period_transition';
            END IF;
            RETURN NEW;
        END IF;

        IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'reporting period % is published and cannot change', OLD.id
                USING ERRCODE = 'check_violation', CONSTRAINT = 'period_published';
        END IF;

        IF OLD.status <> 'open'
           AND (NEW.name, NEW.start_date, NEW.end_date)
               IS DISTINCT FROM (OLD.name, OLD.start_date, OLD.end_date) THEN
            RAISE EXCEPTION 'reporting period % is locked; unlock it to edit', OLD.id
                USING ERRCODE = 'check_violation', CONSTRAINT = 'period_locked';
        END IF;

        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF NOT ((OLD.status = 'open' AND NEW.status = 'locked')
                    OR (OLD.status = 'locked' AND NEW.status IN ('open', 'published'))) THEN
                RAISE EXCEPTION 'reporting period cannot go from % to %', OLD.status, NEW.status
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'period_transition';
            END IF;
            IF NOT (app_is_platform_admin() OR {_ORG_ADMIN_OF_NEW_ORG}) THEN
                RAISE EXCEPTION 'only a platform admin or org admin can change period status'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER reporting_period_guard BEFORE INSERT OR UPDATE ON reporting_period"
    " FOR EACH ROW EXECUTE FUNCTION reporting_period_guard()",
    # --- role_assignment: tenant table. Only platform admins grant or revoke auditors.
    # Users also see their own assignments in other organizations (for "my organizations").
    "ALTER TABLE role_assignment ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE role_assignment FORCE ROW LEVEL SECURITY",
    "CREATE POLICY role_assignment_select ON role_assignment FOR SELECT"
    " USING (organization_id = app_current_org_id() OR user_id = app_current_user_id())",
    "CREATE POLICY role_assignment_insert ON role_assignment FOR INSERT WITH CHECK ("
    " organization_id = app_current_org_id() AND (role <> 'auditor' OR app_is_platform_admin()))",
    "CREATE POLICY role_assignment_update ON role_assignment FOR UPDATE"
    " USING (organization_id = app_current_org_id()"
    " AND (role <> 'auditor' OR app_is_platform_admin()))"
    " WITH CHECK (organization_id = app_current_org_id()"
    " AND (role <> 'auditor' OR app_is_platform_admin()))",
    "GRANT SELECT, INSERT, UPDATE (revoked_at, revoked_by) ON role_assignment TO gri_app",
    # --- Auth functions. SECURITY DEFINER: they run as the owner, before any request context
    # exists, and expose only what login and token checks need. Never password hashes in bulk.
    """
    CREATE FUNCTION auth_login_lookup(p_email text)
    RETURNS TABLE (user_id uuid, password_hash text, is_active boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        SELECT u.id, c.password_hash, u.is_active
        FROM app_user u LEFT JOIN password_credential c ON c.user_id = u.id
        WHERE lower(u.email) = lower(p_email)
    $$
    """,
    """
    CREATE FUNCTION auth_resolve_session(p_token_hash bytea)
    RETURNS TABLE (user_id uuid, is_platform_admin boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        SELECT s.user_id,
               EXISTS (
                   SELECT 1 FROM platform_role_assignment p
                   WHERE p.user_id = s.user_id AND p.role = 'platform_admin'
                     AND p.revoked_at IS NULL
               )
        FROM auth_session s JOIN app_user u ON u.id = s.user_id
        WHERE s.token_hash = p_token_hash
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND u.is_active
    $$
    """,
    """
    CREATE FUNCTION auth_set_password(p_user_id uuid, p_password_hash text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    BEGIN
        IF NOT (p_user_id = app_current_user_id() OR app_is_platform_admin()) THEN
            RAISE EXCEPTION 'not allowed to set this password'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        INSERT INTO password_credential (user_id, password_hash, changed_at)
        VALUES (p_user_id, p_password_hash, now())
        ON CONFLICT (user_id)
        DO UPDATE SET password_hash = EXCLUDED.password_hash, changed_at = now();
    END
    $$
    """,
    """
    CREATE FUNCTION app_user_id_by_email(p_email text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        SELECT id FROM app_user WHERE lower(email) = lower(p_email) AND is_active
    $$
    """,
    *(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC" for fn in _AUTH_FUNCTIONS),
    *(f"GRANT EXECUTE ON FUNCTION {fn} TO gri_app" for fn in _AUTH_FUNCTIONS),
)

UNDO_SECURITY: tuple[str, ...] = (
    *(f"DROP FUNCTION {fn}" for fn in _AUTH_FUNCTIONS),
    # These policies reference role_assignment, which would block dropping it.
    "DROP POLICY organization_select ON organization",
    "DROP POLICY app_user_select ON app_user",
    "DROP FUNCTION reporting_period_guard() CASCADE",
    "DROP FUNCTION entity_prevent_cycle() CASCADE",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")  # for the period overlap EXCLUDE
    op.execute(SET_UPDATED_AT)

    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "app_user",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(display_name) <> ''", name=op.f("ck_app_user_display_name_not_blank")
        ),
        sa.CheckConstraint(
            "email ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'", name=op.f("ck_app_user_email_format")
        ),
        sa.CheckConstraint("phone ~ '^\\+[1-9][0-9]{6,14}$'", name=op.f("ck_app_user_phone_e164")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
    )
    op.create_index(
        "uq_app_user_email_lower", "app_user", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "organization",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_organization_name_not_blank")),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name=op.f("ck_organization_slug_format")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name=op.f("ck_organization_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization")),
        sa.UniqueConstraint("slug", name=op.f("uq_organization_slug")),
    )
    op.create_table(
        "auth_session",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_auth_session_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_session_token_hash")),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"], unique=False)
    op.create_table(
        "entity",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(code) <> ''", name=op.f("ck_entity_code_not_blank")),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_entity_name_not_blank")),
        sa.CheckConstraint(
            "kind IN ('group', 'legal_entity', 'business_unit', 'site')",
            name=op.f("ck_entity_kind_valid"),
        ),
        sa.CheckConstraint("parent_id <> id", name=op.f("ck_entity_not_own_parent")),
        sa.ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["entity.organization_id", "entity.id"],
            name=op.f("fk_entity_organization_id_entity"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_entity_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity")),
        sa.UniqueConstraint("organization_id", "code", name=op.f("uq_entity_organization_id_code")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_entity_organization_id_id")),
    )
    op.create_table(
        "password_credential",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_password_credential_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_password_credential")),
    )
    op.create_table(
        "platform_role_assignment",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role IN ('platform_admin')", name=op.f("ck_platform_role_assignment_role_valid")
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name=op.f("ck_platform_role_assignment_revoke_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["app_user.id"],
            name=op.f("fk_platform_role_assignment_granted_by_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["app_user.id"],
            name=op.f("fk_platform_role_assignment_revoked_by_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_platform_role_assignment_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_role_assignment")),
    )
    op.create_index(
        "uq_platform_role_assignment_active",
        "platform_role_assignment",
        ["user_id", "role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "reporting_period",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        postgresql.ExcludeConstraint(
            (sa.column("organization_id"), "="),
            (sa.text("daterange(start_date, end_date, '[]')"), "&&"),
            using="gist",
            name="ex_reporting_period_no_overlap",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND locked_at IS NULL AND locked_by IS NULL AND published_at IS NULL AND published_by IS NULL) OR (status = 'locked' AND locked_at IS NOT NULL AND locked_by IS NOT NULL AND published_at IS NULL AND published_by IS NULL) OR (status = 'published' AND locked_at IS NOT NULL AND locked_by IS NOT NULL AND published_at IS NOT NULL AND published_by IS NOT NULL)",
            name=op.f("ck_reporting_period_status_fields_consistent"),
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_reporting_period_name_not_blank")),
        sa.CheckConstraint(
            "status IN ('open', 'locked', 'published')",
            name=op.f("ck_reporting_period_status_valid"),
        ),
        sa.CheckConstraint(
            "end_date >= start_date", name=op.f("ck_reporting_period_dates_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["locked_by"], ["app_user.id"], name=op.f("fk_reporting_period_locked_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_reporting_period_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["published_by"],
            ["app_user.id"],
            name=op.f("fk_reporting_period_published_by_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reporting_period")),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_reporting_period_organization_id_id")
        ),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_reporting_period_organization_id_name")
        ),
    )
    op.create_table(
        "role_assignment",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "entity_id IS NULL OR role NOT IN ('org_admin', 'auditor')",
            name=op.f("ck_role_assignment_org_wide_roles_unscoped"),
        ),
        sa.CheckConstraint(
            "role IN ('org_admin', 'contributor', 'approver', 'viewer', 'auditor')",
            name=op.f("ck_role_assignment_role_valid"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name=op.f("ck_role_assignment_revoke_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["app_user.id"], name=op.f("fk_role_assignment_granted_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "entity_id"],
            ["entity.organization_id", "entity.id"],
            name=op.f("fk_role_assignment_organization_id_entity"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_role_assignment_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"], ["app_user.id"], name=op.f("fk_role_assignment_revoked_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_role_assignment_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignment")),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_role_assignment_organization_id_id")
        ),
    )
    op.create_index("ix_role_assignment_user_id", "role_assignment", ["user_id"], unique=False)
    op.create_index(
        "uq_role_assignment_active",
        "role_assignment",
        ["organization_id", "user_id", "role", "entity_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    # ### end Alembic commands ###

    for statement in SECURITY:
        op.execute(statement)


def downgrade() -> None:
    for statement in UNDO_SECURITY:
        op.execute(statement)

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        "uq_role_assignment_active",
        table_name="role_assignment",
        postgresql_where=sa.text("revoked_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.drop_index("ix_role_assignment_user_id", table_name="role_assignment")
    op.drop_table("role_assignment")
    op.drop_table("reporting_period")
    op.drop_index(
        "uq_platform_role_assignment_active",
        table_name="platform_role_assignment",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("platform_role_assignment")
    op.drop_table("password_credential")
    op.drop_table("entity")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_table("organization")
    op.drop_index("uq_app_user_email_lower", table_name="app_user")
    op.drop_table("app_user")
    # ### end Alembic commands ###
    op.execute("DROP FUNCTION set_updated_at()")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
