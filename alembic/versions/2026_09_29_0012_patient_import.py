"""Patient import batches, and the one way imported patients can be removed: undo within 30
minutes, only while nothing has been recorded against them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Everything that counts as "recorded against a patient". An import cannot be undone once
# any of these exist for one of its patients.
RECORD_TABLES = [
    "consultations", "treatments", "arrivals", "follow_ups", "files", "tooth_findings", "mouth_checks",
    "prescriptions", "invoices", "estimates",
]

UNDO_FUNCTION = f"""
CREATE FUNCTION undo_patient_import(p_batch uuid, p_by uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_batch import_batches%ROWTYPE;
  v_removed integer;
BEGIN
  -- Runs as the table owner, but row level security still applies (FORCE), so only the
  -- clinic set for this transaction is visible.
  SELECT * INTO v_batch FROM import_batches WHERE id = p_batch FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'import not found' USING ERRCODE = 'DN404';
  END IF;
  IF v_batch.undone_at IS NOT NULL THEN
    RAISE EXCEPTION 'import already undone' USING ERRCODE = 'DN409';
  END IF;
  IF now() > v_batch.undo_deadline THEN
    RAISE EXCEPTION 'undo time is over' USING ERRCODE = 'DN410';
  END IF;
  IF {" OR ".join(f"EXISTS (SELECT 1 FROM {t} x JOIN patients p ON p.id = x.patient_id WHERE p.import_batch_id = p_batch)" for t in RECORD_TABLES)}
     OR EXISTS (SELECT 1 FROM ledger_entries x JOIN patients p ON p.id = x.patient_id
                WHERE p.import_batch_id = p_batch AND x.kind <> 'opening_balance') THEN
    RAISE EXCEPTION 'something has been recorded for these patients' USING ERRCODE = 'DN423';
  END IF;
  DELETE FROM ledger_entries WHERE kind = 'opening_balance'
    AND patient_id IN (SELECT id FROM patients WHERE import_batch_id = p_batch);
  DELETE FROM patients WHERE import_batch_id = p_batch;
  GET DIAGNOSTICS v_removed = ROW_COUNT;
  UPDATE import_batches SET undone_at = now(), undone_by = p_by WHERE id = p_batch;
  RETURN v_removed;
END $$;
"""


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        # The uploaded Excel or CSV file, kept in file storage.
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rows_total", sa.Integer, nullable=False),
        sa.Column("rows_imported", sa.Integer, nullable=False),
        sa.Column("rows_skipped", sa.Integer, nullable=False),
        sa.Column("kept_numbers", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("undo_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
        sa.Column("undone_by", postgresql.UUID(as_uuid=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="import_batches_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "file_id"], ["files.clinic_id", "files.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "uploaded_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "undone_by"], ["staff.clinic_id", "staff.id"]),
    )
    op.execute("ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_batches FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY clinic_isolation ON import_batches "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    op.execute("REVOKE ALL ON import_batches FROM denti_app")
    op.execute("GRANT SELECT, INSERT ON import_batches TO denti_app")

    for table in ("patients", "import_batches"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.create_foreign_key(
        "patients_import_batch", "patients", "import_batches", ["clinic_id", "import_batch_id"], ["clinic_id", "id"]
    )
    # Registration still needs a date of birth or an age; a patient list from other software
    # often has neither, so imported patients may come without.
    op.drop_constraint("patients_dob_or_birth_year", "patients", type_="check")
    op.create_check_constraint(
        "patients_dob_or_birth_year", "patients", "dob IS NOT NULL OR birth_year IS NOT NULL OR import_batch_id IS NOT NULL"
    )
    for table in ("patients", "import_batches"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # The app role has no DELETE on patients or the bill. Undoing an import goes only through
    # this function, which checks the 30 minutes and that nothing was recorded.
    op.execute(UNDO_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION undo_patient_import(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION undo_patient_import(uuid, uuid) TO denti_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION undo_patient_import(uuid, uuid)")
    op.drop_constraint("patients_dob_or_birth_year", "patients", type_="check")
    op.create_check_constraint("patients_dob_or_birth_year", "patients", "dob IS NOT NULL OR birth_year IS NOT NULL")
    op.drop_constraint("patients_import_batch", "patients", type_="foreignkey")
    op.drop_table("import_batches")
