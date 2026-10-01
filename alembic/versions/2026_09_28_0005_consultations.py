"""Consultations, prescriptions with every version kept, clinic settings, and the patient's
running bill (ledger entries).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _clinic() -> sa.Column:
    return sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _protect(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY clinic_isolation ON {table} "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {settings.db_app_role}")
    op.execute(f"GRANT {grants} ON {table} TO {settings.db_app_role}")


def upgrade() -> None:
    op.create_table(
        "clinic_settings",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), primary_key=True),
        sa.Column("default_consultation_fee_paise", sa.BigInteger, nullable=False, server_default="50000"),
        sa.Column("estimate_valid_days", sa.Integer, nullable=False, server_default="30"),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("default_consultation_fee_paise >= 0", name="clinic_settings_fee_not_negative"),
    )
    _protect("clinic_settings", "SELECT, INSERT, UPDATE")

    op.create_table(
        "consultations",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        _uuid("doctor_id", nullable=False),
        sa.Column("consult_date", sa.Date, nullable=False),
        sa.Column("chief_complaint", sa.Text, nullable=False),
        sa.Column("oral_examination", sa.Text, nullable=False),
        sa.Column("xray_taken", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("recommendation_note", sa.Text),
        _uuid("created_by", nullable=False),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="consultations_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "doctor_id"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
    )
    op.create_index("consultations_patient", "consultations", ["clinic_id", "patient_id", "consult_date"])
    # Medical records are never deleted; edits keep their before and after in the audit log.
    _protect("consultations", "SELECT, INSERT, UPDATE")

    op.create_table(
        "consultation_recommendations",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("consultation_id", nullable=False),
        _uuid("service_id", nullable=False),
        sa.Column("teeth", postgresql.ARRAY(sa.SmallInteger), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
    )
    op.create_index("consultation_recommendations_consultation", "consultation_recommendations", ["clinic_id", "consultation_id"])
    # Recommendations are replaced as a set when a consultation is edited.
    _protect("consultation_recommendations", "SELECT, INSERT, DELETE")

    op.create_table(
        "prescriptions",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        _uuid("consultation_id"),
        _uuid("visit_id"),
        _uuid("prescribing_doctor_id", nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="1"),
        _uuid("created_by", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="prescriptions_clinic_id_id_key"),
        sa.UniqueConstraint("consultation_id", name="prescriptions_one_per_consultation"),
        sa.UniqueConstraint("visit_id", name="prescriptions_one_per_visit"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "prescribing_doctor_id"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint("(consultation_id IS NOT NULL) <> (visit_id IS NOT NULL)", name="prescriptions_one_source"),
    )
    _protect("prescriptions", "SELECT, INSERT, UPDATE")

    op.create_table(
        "prescription_versions",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("prescription_id", nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("diagnosis", sa.Text),
        sa.Column("advice", sa.Text),
        sa.Column("items", postgresql.JSONB, nullable=False, server_default="[]"),
        _uuid("edited_by", nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prescription_id", "version", name="prescription_versions_number"),
        sa.ForeignKeyConstraint(["clinic_id", "prescription_id"], ["prescriptions.clinic_id", "prescriptions.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "edited_by"], ["staff.clinic_id", "staff.id"]),
    )
    # Every version is kept exactly as written: added, never changed.
    _protect("prescription_versions", "SELECT, INSERT")

    op.create_table(
        "ledger_entries",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        # Positive: the patient owes more. Negative: paid or reduced.
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        _uuid("consultation_id"),
        _uuid("treatment_id"),
        sa.Column("payment_mode", sa.String(40)),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        _uuid("reverses_entry_id"),
        sa.Column("note", sa.String(300)),
        _uuid("created_by", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint(
            "kind IN ('consultation_fee', 'treatment_charge', 'price_change', 'opening_balance', "
            "'payment', 'payment_reversal', 'refund')",
            name="ledger_entries_kind",
        ),
    )
    op.create_index("ledger_entries_patient", "ledger_entries", ["clinic_id", "patient_id", "effective_at"])
    op.create_index("ledger_entries_clinic_time", "ledger_entries", ["clinic_id", "effective_at"])
    # The bill is append only: a mistake is corrected by a new entry, never by editing or deleting one.
    _protect("ledger_entries", "SELECT, INSERT")


def downgrade() -> None:
    for table in [
        "ledger_entries", "prescription_versions", "prescriptions", "consultation_recommendations",
        "consultations", "clinic_settings",
    ]:
        op.drop_table(table)
