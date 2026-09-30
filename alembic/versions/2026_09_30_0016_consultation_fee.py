"""A consultation keeps its own fee, increase and discount, like a treatment, so the owner or
receptionist can change what one consultation costs from the bill (a free review visit, a
discount), with only the difference added to the bill.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("consultations", sa.Column("fee_paise", sa.BigInteger, nullable=False, server_default="0"))
    # Increase only, then a discount on the increased fee. Percent is in basis points.
    op.add_column("consultations", sa.Column("fee_adjustment_type", sa.String(7)))
    op.add_column("consultations", sa.Column("fee_adjustment_value", sa.BigInteger, nullable=False, server_default="0"))
    op.add_column("consultations", sa.Column("fee_discount_type", sa.String(7)))
    op.add_column("consultations", sa.Column("fee_discount_value", sa.BigInteger, nullable=False, server_default="0"))
    op.create_check_constraint(
        "consultations_fee_adjustment_type", "consultations", "fee_adjustment_type IS NULL OR fee_adjustment_type IN ('percent', 'amount')"
    )
    op.create_check_constraint(
        "consultations_fee_discount_type", "consultations", "fee_discount_type IS NULL OR fee_discount_type IN ('percent', 'amount')"
    )
    op.create_check_constraint(
        "consultations_fee_not_negative", "consultations", "fee_paise >= 0 AND fee_adjustment_value >= 0 AND fee_discount_value >= 0"
    )
    op.create_check_constraint(
        "consultations_fee_discount_at_most_all", "consultations", "fee_discount_type <> 'percent' OR fee_discount_value <= 10000"
    )

    # Consultations already saved keep the fee they were charged. The migration role lifts
    # FORCE for this one step.
    for table in ("consultations", "ledger_entries"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE consultations c SET fee_paise = charged.amount FROM ("
        " SELECT consultation_id, sum(amount_paise) AS amount FROM ledger_entries"
        " WHERE kind = 'consultation_fee' AND consultation_id IS NOT NULL GROUP BY consultation_id"
        ") charged WHERE charged.consultation_id = c.id"
    )
    for table in ("consultations", "ledger_entries"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for name in (
        "consultations_fee_discount_at_most_all", "consultations_fee_not_negative",
        "consultations_fee_discount_type", "consultations_fee_adjustment_type",
    ):
        op.drop_constraint(name, "consultations", type_="check")
    for column in ("fee_discount_value", "fee_discount_type", "fee_adjustment_value", "fee_adjustment_type", "fee_paise"):
        op.drop_column("consultations", column)
