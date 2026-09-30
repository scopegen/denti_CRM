"""Invoices: printable documents for what is on a patient's bill, numbered per financial year.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _clinic() -> sa.Column:
    return sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False)


def _protect(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY clinic_isolation ON {table} "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    op.execute(f"REVOKE ALL ON {table} FROM denti_app")
    op.execute(f"GRANT {grants} ON {table} TO denti_app")


def upgrade() -> None:
    op.create_table(
        "invoices",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        # The running number within the financial year, and the year itself: 2026-27 runs
        # from 1 April 2026 to 31 March 2027. Printed as SMILE/26-27/0001.
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("financial_year", sa.String(7), nullable=False),
        sa.Column("issued_on", sa.Date, nullable=False),
        _uuid("issued_by", nullable=False),
        sa.Column("total_paise", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="issued"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        _uuid("cancelled_by"),
        sa.Column("cancel_reason", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="invoices_clinic_id_id_key"),
        # A cancelled invoice keeps its number, so no number is ever used twice.
        sa.UniqueConstraint("clinic_id", "financial_year", "number", name="invoices_number_per_year"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "issued_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint("status IN ('issued', 'cancelled')", name="invoices_status"),
        sa.CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="invoices_cancelled_at"),
        sa.CheckConstraint("total_paise >= 0", name="invoices_total_not_negative"),
    )
    op.create_index("invoices_patient", "invoices", ["clinic_id", "patient_id", "issued_on"])
    # Never deleted; cancelling is an update that keeps the number.
    _protect("invoices", "SELECT, INSERT, UPDATE")

    op.create_table(
        "invoice_lines",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("invoice_id", nullable=False),
        _uuid("consultation_id"),
        _uuid("treatment_id"),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("teeth", postgresql.ARRAY(sa.SmallInteger), nullable=False, server_default="{}"),
        # What the bill holds for the item: the final price, after any discount.
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "invoice_id"], ["invoices.clinic_id", "invoices.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        sa.CheckConstraint("(consultation_id IS NULL) <> (treatment_id IS NULL)", name="invoice_lines_one_item"),
    )
    op.create_index("invoice_lines_invoice", "invoice_lines", ["clinic_id", "invoice_id"])
    _protect("invoice_lines", "SELECT, INSERT")


def downgrade() -> None:
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
