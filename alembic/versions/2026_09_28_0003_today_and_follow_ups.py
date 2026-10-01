"""Walk ins (the Today page) and follow ups (calls and expected visits).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "arrivals",
        _uuid("id", nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        _uuid("patient_id", nullable=False),
        # The day in the clinic's own time zone, so "today" never shifts at midnight UTC.
        sa.Column("clinic_date", sa.Date, nullable=False),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _uuid("preferred_doctor_id"),
        _uuid("marked_by", nullable=False),
        # Filled by the server when a consultation or visit is saved for this patient that day.
        sa.Column("seen_at", sa.DateTime(timezone=True)),
        _uuid("seen_by_consultation_id"),
        _uuid("seen_by_visit_id"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        _uuid("cancelled_by"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "preferred_doctor_id"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "marked_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
    )
    op.create_index("arrivals_clinic_date", "arrivals", ["clinic_id", "clinic_date", "arrived_at"])
    op.create_index("arrivals_patient", "arrivals", ["clinic_id", "patient_id", "clinic_date"])

    op.create_table(
        "follow_ups",
        _uuid("id", nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        _uuid("patient_id", nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("due_on", sa.Date, nullable=False),
        sa.Column("due_time", sa.Time),
        sa.Column("reason", sa.String(200)),
        sa.Column("status", sa.String(12), nullable=False, server_default="upcoming"),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        _uuid("created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        _uuid("completed_by"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "completed_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint("kind IN ('call', 'visit')", name="follow_ups_kind"),
        sa.CheckConstraint("status IN ('upcoming', 'done', 'cancelled')", name="follow_ups_status"),
        sa.CheckConstraint(
            "source IN ('manual', 'after_visit', 'recall', 'lab_received', 'arrival', 'import')", name="follow_ups_source"
        ),
    )
    op.create_index("follow_ups_due", "follow_ups", ["clinic_id", "status", "due_on"])
    op.create_index("follow_ups_patient", "follow_ups", ["clinic_id", "patient_id", "due_on"])

    for table in ["arrivals", "follow_ups"]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY clinic_isolation ON {table} "
            "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
            "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
        )
        # Nothing is deleted: a mistaken arrival is cancelled, a follow up is marked cancelled.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {settings.db_app_role}")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {settings.db_app_role}")


def downgrade() -> None:
    op.drop_table("follow_ups")
    op.drop_table("arrivals")
