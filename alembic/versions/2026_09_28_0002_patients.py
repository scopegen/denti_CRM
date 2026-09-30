"""Patients, per clinic number counters and the lists each clinic edits.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CLINIC_TABLES = ["counters", "picklists", "patients"]

# The same dental relevant checklist Ranco uses; every clinic can edit its own copy.
DEFAULT_MEDICAL_CONDITIONS = [
    "Diabetes",
    "Hypertension (High Blood Pressure)",
    "Heart Disease",
    "Bleeding Disorder / Blood Thinners",
    "Asthma",
    "Epilepsy / Seizure Disorder",
    "Thyroid Disorder",
    "Pregnancy",
    "Allergy to Medication (e.g. Penicillin)",
    "Smoking / Tobacco Use",
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "counters",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), primary_key=True),
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("period", sa.String(10), primary_key=True),
        sa.Column("next_value", sa.Integer, nullable=False, server_default="1"),
        sa.CheckConstraint("kind IN ('patient', 'invoice', 'estimate')", name="counters_kind"),
    )

    op.create_table(
        "picklists",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("list", sa.String(30), nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "list IN ('medical_condition', 'tooth_problem', 'payment_mode', 'city', 'lab_material')",
            name="picklists_list",
        ),
    )
    op.create_index("picklists_clinic_list_value", "picklists", ["clinic_id", "list", sa.text("lower(value)")], unique=True)

    op.create_table(
        "patients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("patient_number", sa.Integer, nullable=False),
        sa.Column("previous_id", sa.String(40)),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("gender", sa.String(10)),
        sa.Column("dob", sa.Date),
        sa.Column("birth_year", sa.Integer),
        sa.Column("city", sa.String(80)),
        sa.Column("area", sa.String(80)),
        sa.Column("height_cm", sa.Numeric(5, 1)),
        sa.Column("weight_kg", sa.Numeric(5, 1)),
        sa.Column("medical_conditions", postgresql.ARRAY(sa.String(120)), nullable=False, server_default="{}"),
        sa.Column("medical_history", sa.Text),
        sa.Column("whatsapp_consent_at", sa.DateTime(timezone=True)),
        sa.Column("registered_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True)),
        sa.Column("merged_into_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("clinic_id", "id", name="patients_clinic_id_id_key"),
        sa.UniqueConstraint("clinic_id", "patient_number", name="patients_clinic_number_key"),
        sa.ForeignKeyConstraint(["clinic_id", "registered_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "merged_into_id"], ["patients.clinic_id", "patients.id"]),
        sa.CheckConstraint("gender IS NULL OR gender IN ('male', 'female', 'other')", name="patients_gender"),
        sa.CheckConstraint("dob IS NOT NULL OR birth_year IS NOT NULL", name="patients_dob_or_birth_year"),
    )
    op.create_index("patients_clinic_phone", "patients", ["clinic_id", "phone"])
    op.create_index("patients_clinic_created", "patients", ["clinic_id", "created_at"])
    op.execute("CREATE INDEX patients_name_trgm ON patients USING gin (lower(name) gin_trgm_ops)")

    for table in NEW_CLINIC_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY clinic_isolation ON {table} "
            "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
            "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
        )
        # Patients are never deleted (they can only be merged), and nothing here is.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO denti_app")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM denti_app")

    # Give every existing clinic the default medical conditions checklist.
    connection = op.get_bind()
    for (clinic_id,) in connection.execute(sa.text("SELECT id FROM clinics")).all():
        connection.execute(sa.text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        for order, value in enumerate(DEFAULT_MEDICAL_CONDITIONS):
            connection.execute(
                sa.text(
                    "INSERT INTO picklists (id, clinic_id, list, value, sort_order) "
                    "VALUES (gen_random_uuid(), :cid, 'medical_condition', :value, :order)"
                ),
                {"cid": clinic_id, "value": value, "order": order},
            )


def downgrade() -> None:
    for table in ["patients", "picklists", "counters"]:
        op.drop_table(table)
