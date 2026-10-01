"""Treatments, their visits (one per sitting), handoffs between doctors, and links from the
bill, prescriptions and follow ups to them.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _clinic() -> sa.Column:
    return sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False)


def _staff_fk(column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["clinic_id", column], ["staff.clinic_id", "staff.id"])


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
        "treatments",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        _uuid("service_id", nullable=False),
        # The doctor in charge now; earlier ones are in treatment_handoffs.
        _uuid("doctor_id", nullable=False),
        sa.Column("teeth", postgresql.ARRAY(sa.SmallInteger), nullable=False, server_default="{}"),
        # Where it came from, if anywhere. Any treatment can be started without either.
        _uuid("consultation_id"),
        _uuid("estimate_item_id"),
        sa.Column("status", sa.String(10), nullable=False, server_default="planned"),
        # A snapshot: changing the service's price later never changes this.
        sa.Column("price_paise", sa.BigInteger, nullable=False),
        # Increase only, then a discount on the increased price. Percent is in basis points.
        sa.Column("adjustment_type", sa.String(7)),
        sa.Column("adjustment_value", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("discount_type", sa.String(7)),
        sa.Column("discount_value", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text),
        sa.Column("started_on", sa.Date),
        sa.Column("finished_on", sa.Date),
        sa.Column("cancelled_on", sa.Date),
        sa.Column("cancel_reason", sa.String(300)),
        _uuid("created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="treatments_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        _staff_fk("doctor_id"),
        _staff_fk("created_by"),
        sa.CheckConstraint("status IN ('planned', 'ongoing', 'finished', 'cancelled')", name="treatments_status"),
        # The dates follow the status, so no screen can leave them out of step.
        sa.CheckConstraint("status <> 'planned' OR started_on IS NULL", name="treatments_planned_not_started"),
        sa.CheckConstraint("status NOT IN ('ongoing', 'finished') OR started_on IS NOT NULL", name="treatments_started_on"),
        sa.CheckConstraint("(status = 'finished') = (finished_on IS NOT NULL)", name="treatments_finished_on"),
        sa.CheckConstraint("(status = 'cancelled') = (cancelled_on IS NOT NULL)", name="treatments_cancelled_on"),
        sa.CheckConstraint("finished_on IS NULL OR finished_on >= started_on", name="treatments_finish_after_start"),
        sa.CheckConstraint("price_paise >= 0", name="treatments_price_not_negative"),
        sa.CheckConstraint("adjustment_type IS NULL OR adjustment_type IN ('percent', 'amount')", name="treatments_adjustment_type"),
        sa.CheckConstraint("discount_type IS NULL OR discount_type IN ('percent', 'amount')", name="treatments_discount_type"),
        sa.CheckConstraint("adjustment_value >= 0 AND discount_value >= 0", name="treatments_changes_not_negative"),
        sa.CheckConstraint("discount_type <> 'percent' OR discount_value <= 10000", name="treatments_discount_at_most_all"),
    )
    op.create_index("treatments_patient", "treatments", ["clinic_id", "patient_id", "status"])
    # Never deleted: a mistake is cancelled, and the change is in the audit log.
    _protect("treatments", "SELECT, INSERT, UPDATE")

    op.create_table(
        "visits",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        _uuid("treatment_id", nullable=False),
        sa.Column("visit_date", sa.Date, nullable=False),
        _uuid("doctor_id", nullable=False),
        # What was done at this sitting.
        sa.Column("notes", sa.Text),
        _uuid("created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="visits_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        _staff_fk("doctor_id"),
        _staff_fk("created_by"),
    )
    op.create_index("visits_treatment", "visits", ["clinic_id", "treatment_id", "visit_date"])
    _protect("visits", "SELECT, INSERT, UPDATE")

    op.create_table(
        "treatment_handoffs",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("treatment_id", nullable=False),
        _uuid("from_doctor_id", nullable=False),
        _uuid("to_doctor_id", nullable=False),
        _uuid("changed_by", nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.String(300)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        _staff_fk("from_doctor_id"),
        _staff_fk("to_doctor_id"),
        _staff_fk("changed_by"),
    )
    _protect("treatment_handoffs", "SELECT, INSERT")

    # Links that could not exist before these tables did.
    op.add_column("follow_ups", _uuid("treatment_id"))
    _link("ledger_entries_treatment", "ledger_entries", "treatment_id", "treatments")
    _link("prescriptions_visit", "prescriptions", "visit_id", "visits")
    _link("follow_ups_treatment", "follow_ups", "treatment_id", "treatments")


def _link(name: str, table: str, column: str, target: str) -> None:
    """A foreign key on a table that may already hold rows. Checking those rows reads both
    tables, and with FORCE the policies would need a clinic set. The migration role owns the
    tables, so it lifts FORCE for this one step, inside this transaction, and puts it back."""
    for t in (table, target):
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    op.create_foreign_key(name, table, target, ["clinic_id", column], ["clinic_id", "id"])
    for t in (table, target):
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_constraint("follow_ups_treatment", "follow_ups", type_="foreignkey")
    op.drop_column("follow_ups", "treatment_id")
    op.drop_constraint("prescriptions_visit", "prescriptions", type_="foreignkey")
    op.drop_constraint("ledger_entries_treatment", "ledger_entries", type_="foreignkey")
    op.drop_table("treatment_handoffs")
    op.drop_table("visits")
    op.drop_table("treatments")
