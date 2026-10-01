"""Treatment estimates (quotes): one or more options of priced items. Accepting an option
turns its items into planned treatments at the quoted prices.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
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
        "estimates",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("financial_year", sa.String(7), nullable=False),
        sa.Column("given_on", sa.Date, nullable=False),
        sa.Column("valid_until", sa.Date, nullable=False),
        # The doctor whose name the estimate carries.
        _uuid("doctor_id", nullable=False),
        # given, then accepted or declined. Expired is worked out from valid_until, not stored.
        sa.Column("status", sa.String(10), nullable=False, server_default="given"),
        # One discount for the whole estimate, taken off each option's total.
        sa.Column("discount_type", sa.String(7)),
        sa.Column("discount_value", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text),
        _uuid("created_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _uuid("accepted_option_id"),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        _uuid("decided_by"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="estimates_clinic_id_id_key"),
        sa.UniqueConstraint("clinic_id", "financial_year", "number", name="estimates_number_per_year"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        _staff_fk("doctor_id"),
        _staff_fk("created_by"),
        _staff_fk("decided_by"),
        sa.CheckConstraint("status IN ('given', 'accepted', 'declined')", name="estimates_status"),
        sa.CheckConstraint("(status = 'given') = (decided_at IS NULL)", name="estimates_decided_at"),
        sa.CheckConstraint("(status = 'accepted') = (accepted_option_id IS NOT NULL)", name="estimates_accepted_option"),
        sa.CheckConstraint("valid_until >= given_on", name="estimates_valid_after_given"),
        sa.CheckConstraint("discount_type IS NULL OR discount_type IN ('percent', 'amount')", name="estimates_discount_type"),
        sa.CheckConstraint("discount_value >= 0", name="estimates_discount_not_negative"),
    )
    op.create_index("estimates_patient", "estimates", ["clinic_id", "patient_id", "given_on"])
    _protect("estimates", "SELECT, INSERT, UPDATE")

    op.create_table(
        "estimate_options",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("estimate_id", nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="estimate_options_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "estimate_id"], ["estimates.clinic_id", "estimates.id"]),
    )
    _protect("estimate_options", "SELECT, INSERT")

    op.create_table(
        "estimate_items",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("option_id", nullable=False),
        _uuid("service_id", nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("teeth", postgresql.ARRAY(sa.SmallInteger), nullable=False, server_default="{}"),
        sa.Column("sittings", sa.SmallInteger),
        sa.Column("unit_price_paise", sa.BigInteger, nullable=False),
        sa.Column("quantity", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "id", name="estimate_items_clinic_id_id_key"),
        sa.ForeignKeyConstraint(["clinic_id", "option_id"], ["estimate_options.clinic_id", "estimate_options.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
        sa.CheckConstraint("unit_price_paise >= 0", name="estimate_items_price_not_negative"),
        sa.CheckConstraint("quantity >= 1", name="estimate_items_quantity"),
        sa.CheckConstraint("sittings IS NULL OR sittings >= 1", name="estimate_items_sittings"),
    )
    _protect("estimate_items", "SELECT, INSERT")

    # The accepted option, and each treatment's estimate item. Adding a foreign key checks the
    # rows already there, which reads both tables; with FORCE the policies would need a clinic
    # set, so the migration role lifts FORCE for these steps only and puts it straight back.
    linked = ("estimates", "estimate_options", "treatments", "estimate_items")
    for table in linked:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.create_foreign_key(
        "estimates_accepted_option_fk", "estimates", "estimate_options", ["clinic_id", "accepted_option_id"], ["clinic_id", "id"]
    )
    op.create_foreign_key(
        "treatments_estimate_item", "treatments", "estimate_items", ["clinic_id", "estimate_item_id"], ["clinic_id", "id"]
    )
    for table in linked:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_constraint("treatments_estimate_item", "treatments", type_="foreignkey")
    op.drop_constraint("estimates_accepted_option_fk", "estimates", type_="foreignkey")
    op.drop_table("estimate_items")
    op.drop_table("estimate_options")
    op.drop_table("estimates")
