"""Stored files (signatures now; X-rays, photos and documents later) and each doctor's signature.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        # Null for clinic files such as a logo or a signature.
        sa.Column("patient_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(20), nullable=False),
        # Where the bytes live: a path under the clinic's own folder, clinics/<clinic_id>/...
        sa.Column("storage_key", sa.String(300), nullable=False, unique=True),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("thumbnail_key", sa.String(300)),
        sa.Column("teeth", postgresql.ARRAY(sa.SmallInteger), nullable=False, server_default="{}"),
        sa.Column("consultation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("visit_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lab_case_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title", sa.String(200)),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # The 30 day bin: a deleted file is hidden, and only removed for good after purge_after.
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("clinic_id", "id", name="files_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "uploaded_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "deleted_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint(
            "kind IN ('xray', 'photo', 'document', 'signature', 'logo', 'import')", name="files_kind"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="files_size_not_negative"),
    )
    op.create_index("files_patient", "files", ["clinic_id", "patient_id", "uploaded_at"])

    op.execute("ALTER TABLE files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY clinic_isolation ON files "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    # Deleting only marks a file as in the bin. Removing it for good is a platform job.
    op.execute("REVOKE ALL ON files FROM denti_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON files TO denti_app")

    op.add_column("staff", sa.Column("signature_file_id", postgresql.UUID(as_uuid=True)))
    # Checking existing staff rows against files reads both tables, and with FORCE the
    # policies would need a clinic set. The migration role owns the tables, so it lifts
    # FORCE for this one step, inside this transaction, and puts it straight back.
    op.execute("ALTER TABLE staff NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files NO FORCE ROW LEVEL SECURITY")
    op.create_foreign_key(
        "staff_signature_file", "staff", "files", ["clinic_id", "signature_file_id"], ["clinic_id", "id"]
    )
    op.execute("ALTER TABLE staff FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_constraint("staff_signature_file", "staff", type_="foreignkey")
    op.drop_column("staff", "signature_file_id")
    op.drop_table("files")
