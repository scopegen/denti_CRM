"""Foundation: clinics, staff, sign in (sessions, emailed codes, trusted devices), audit log.

Revision ID: 0001
Revises:
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that belong to a clinic. Each gets row level security (docs/database-design.md, section 2).
CLINIC_TABLES = ["staff", "auth_sessions", "login_codes", "trusted_devices", "audit_log"]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _clinic_fk() -> sa.Column:
    return sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False)


def _staff_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["clinic_id", "staff_id"], ["staff.clinic_id", "staff.id"])


def upgrade() -> None:
    op.create_table(
        "clinics",
        _uuid_pk(),
        sa.Column("slug", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("plan", sa.String(20), nullable=False, server_default="basic"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("patient_id_prefix", sa.String(12)),
        sa.Column("tooth_numbering", sa.String(10), nullable=False, server_default="fdi"),
        _created_at(),
        sa.CheckConstraint("slug ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'", name="clinics_slug_format"),
        # Clinics live at denti.in/<slug>/..., so Denti's own page names cannot be clinic slugs.
        sa.CheckConstraint(
            "slug NOT IN ('api', 'app', 'admin', 'assets', 'fonts', 'images', 'login', 'signup', "
            "'style-guide', 'www', 'help', 'support', 'pricing', 'about', 'contact', 'terms', 'privacy')",
            name="clinics_slug_not_reserved",
        ),
        sa.CheckConstraint("status IN ('active', 'past_due', 'frozen', 'closed')", name="clinics_status"),
        sa.CheckConstraint("plan IN ('basic', 'standard', 'pro')", name="clinics_plan"),
        sa.CheckConstraint("tooth_numbering IN ('fdi', 'universal')", name="clinics_tooth_numbering"),
    )

    op.create_table(
        "staff",
        _uuid_pk(),
        _clinic_fk(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(20)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("treats_patients", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("qualification", sa.String(120)),
        sa.Column("specialty", sa.String(120)),
        sa.Column("registration_no", sa.String(60)),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
        _created_at(),
        sa.UniqueConstraint("clinic_id", "id", name="staff_clinic_id_id_key"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'doctor')", name="staff_role"),
        sa.CheckConstraint("status IN ('active', 'paused', 'deactivated')", name="staff_status"),
    )
    # An email is unique within a clinic; the same person may work at two clinics.
    op.create_index("staff_clinic_email_key", "staff", ["clinic_id", sa.text("lower(email)")], unique=True)
    # Exactly one active owner per clinic.
    op.create_index(
        "staff_one_active_owner",
        "staff",
        ["clinic_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND status = 'active'"),
    )

    op.create_table(
        "auth_sessions",
        _uuid_pk(),
        _clinic_fk(),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("device", sa.String(200)),
        sa.Column("ip", sa.String(64)),
        _created_at(),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _staff_fk(),
    )
    op.create_index("auth_sessions_staff", "auth_sessions", ["clinic_id", "staff_id"])

    op.create_table(
        "login_codes",
        _uuid_pk(),
        _clinic_fk(),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("sent_to", sa.String(255), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("ip", sa.String(64)),
        sa.Column("device", sa.String(200)),
        _staff_fk(),
        sa.CheckConstraint("purpose IN ('sign_in', 'password_reset')", name="login_codes_purpose"),
    )
    op.create_index("login_codes_staff_created", "login_codes", ["clinic_id", "staff_id", "created_at"])

    op.create_table(
        "trusted_devices",
        _uuid_pk(),
        _clinic_fk(),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("device_name", sa.String(200)),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _staff_fk(),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        _clinic_fk(),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(40)),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True)),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True)),
        sa.Column("changes", postgresql.JSONB),
        sa.Column("ip", sa.String(64)),
        sa.Column("device", sa.Text),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("actor_type IN ('staff', 'platform_support', 'system')", name="audit_log_actor_type"),
    )
    op.create_index("audit_log_clinic_time", "audit_log", ["clinic_id", "occurred_at"])
    op.create_index("audit_log_patient_time", "audit_log", ["clinic_id", "patient_id", "occurred_at"])

    # Row level security: a query only sees rows of the clinic set for the transaction.
    # FORCE makes the rule apply to the table owner too. With no clinic set, queries fail.
    for table in CLINIC_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY clinic_isolation ON {table} "
            "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
            "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
        )

    # What the API role (denti_app) may do. Stated here so every environment gets the same rules.
    op.execute(f"GRANT SELECT ON clinics TO {settings.db_app_role}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON clinics FROM {settings.db_app_role}")
    for table in ["staff", "auth_sessions", "login_codes", "trusted_devices"]:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {settings.db_app_role}")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {settings.db_app_role}")
    # The audit log can be added to and read, never changed or removed.
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {settings.db_app_role}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {settings.db_app_role}")


def downgrade() -> None:
    for table in ["audit_log", "trusted_devices", "login_codes", "auth_sessions", "staff", "clinics"]:
        op.drop_table(table)
