"""WhatsApp tap-to-send: each clinic's message wording, a log of every message opened, and
secure links that let a patient open their prescription, invoice or estimate.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'visit_reminder', 'call_reminder', 'general', 'prescription', 'invoice', 'estimate', 'review_request', 'lab_ready'"
)

# Undoing a patient import (0012) must also stop once a message or a link exists for one of
# its patients.
RECORD_TABLES_BEFORE = [
    "consultations", "treatments", "arrivals", "follow_ups", "files", "tooth_findings", "mouth_checks",
    "prescriptions", "invoices", "estimates",
]
RECORD_TABLES = RECORD_TABLES_BEFORE + ["message_log", "share_links"]


def _undo_function(tables: list[str]) -> str:
    recorded = " OR ".join(
        f"EXISTS (SELECT 1 FROM {t} x JOIN patients p ON p.id = x.patient_id WHERE p.import_batch_id = p_batch)"
        for t in tables
    )
    return f"""
CREATE OR REPLACE FUNCTION undo_patient_import(p_batch uuid, p_by uuid) RETURNS integer
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
  IF {recorded}
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
    op.add_column("clinic_settings", sa.Column("whatsapp_language", sa.String(2), nullable=False, server_default="en"))
    op.add_column("clinic_settings", sa.Column("google_review_url", sa.String(500)))
    op.create_check_constraint("clinic_settings_whatsapp_language", "clinic_settings", "whatsapp_language IN ('en', 'hi')")

    # Only the wording a clinic changed is stored; everything else uses Denti's default.
    op.create_table(
        "message_templates",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("language", sa.String(2), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("clinic_id", "kind", "language"),
        sa.ForeignKeyConstraint(["clinic_id", "updated_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="message_templates_kind"),
        sa.CheckConstraint("language IN ('en', 'hi')", name="message_templates_language"),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 1000", name="message_templates_body_length"),
    )
    # DELETE puts a message back to Denti's default wording.
    _protect("message_templates", "SELECT, INSERT, UPDATE, DELETE")

    # A link in a message lets the patient open one document without signing in. The token is
    # stored hashed; the link stops working when it expires or is stopped.
    op.create_table(
        "share_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prescription_id", postgresql.UUID(as_uuid=True)),
        # The version sent; later edits do not change what the patient was given.
        sa.Column("prescription_version", sa.Integer),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True)),
        sa.Column("estimate_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True)),
        sa.Column("open_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_opened_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="share_links_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "prescription_id"], ["prescriptions.clinic_id", "prescriptions.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "invoice_id"], ["invoices.clinic_id", "invoices.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "estimate_id"], ["estimates.clinic_id", "estimates.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "revoked_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint(
            "(kind = 'prescription' AND prescription_id IS NOT NULL AND prescription_version IS NOT NULL"
            " AND invoice_id IS NULL AND estimate_id IS NULL)"
            " OR (kind = 'invoice' AND invoice_id IS NOT NULL AND prescription_id IS NULL AND estimate_id IS NULL)"
            " OR (kind = 'estimate' AND estimate_id IS NOT NULL AND prescription_id IS NULL AND invoice_id IS NULL)",
            name="share_links_one_document",
        ),
        sa.CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="share_links_revoked_by"),
    )
    _protect("share_links", "SELECT, INSERT, UPDATE")

    # Messages point at follow ups, which had no (clinic_id, id) key to point at.
    op.create_unique_constraint("follow_ups_clinic_id_id_key", "follow_ups", ["clinic_id", "id"])

    # Every WhatsApp message opened from Denti. Tap-to-send can only know the message was
    # opened; the automatic version later records sent, delivered, read or failed.
    op.create_table(
        "message_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("language", sa.String(2), nullable=False),
        sa.Column("method", sa.String(10), nullable=False, server_default="tap"),
        sa.Column("status", sa.String(12), nullable=False, server_default="opened"),
        sa.Column("to_phone", sa.String(20), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("share_link_id", postgresql.UUID(as_uuid=True)),
        sa.Column("follow_up_id", postgresql.UUID(as_uuid=True)),
        sa.Column("treatment_id", postgresql.UUID(as_uuid=True)),
        sa.Column("provider_message_id", sa.String(100)),
        sa.Column("sent_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "share_link_id"], ["share_links.clinic_id", "share_links.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "follow_up_id"], ["follow_ups.clinic_id", "follow_ups.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "sent_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="message_log_kind"),
        sa.CheckConstraint("language IN ('en', 'hi')", name="message_log_language"),
        sa.CheckConstraint("method IN ('tap', 'api')", name="message_log_method"),
        sa.CheckConstraint("status IN ('opened', 'sent', 'delivered', 'read', 'failed')", name="message_log_status"),
        sa.CheckConstraint("method = 'api' OR sent_by IS NOT NULL", name="message_log_tap_has_sender"),
    )
    op.create_index("message_log_patient", "message_log", ["clinic_id", "patient_id", "created_at"])
    _protect("message_log", "SELECT, INSERT")

    # A patient opening a shared link is recorded in the Activity log too.
    op.drop_constraint("audit_log_actor_type", "audit_log", type_="check")
    op.create_check_constraint(
        "audit_log_actor_type", "audit_log", "actor_type IN ('staff', 'platform_support', 'system', 'patient')"
    )

    op.execute(_undo_function(RECORD_TABLES))


def downgrade() -> None:
    op.execute(_undo_function(RECORD_TABLES_BEFORE))
    op.drop_constraint("audit_log_actor_type", "audit_log", type_="check")
    # The log is never rewritten, so rows a patient's link already added stay as they are.
    op.execute(
        "ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_type "
        "CHECK (actor_type IN ('staff', 'platform_support', 'system')) NOT VALID"
    )
    op.drop_table("message_log")
    op.drop_constraint("follow_ups_clinic_id_id_key", "follow_ups", type_="unique")
    op.drop_table("share_links")
    op.drop_table("message_templates")
    op.drop_constraint("clinic_settings_whatsapp_language", "clinic_settings", type_="check")
    op.drop_column("clinic_settings", "google_review_url")
    op.drop_column("clinic_settings", "whatsapp_language")
