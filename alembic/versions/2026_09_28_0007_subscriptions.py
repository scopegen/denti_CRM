"""How each clinic pays for its plan: Starter pack, monthly or yearly, and extra storage packs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        # One per clinic. The plan in effect stays on clinics.plan; this row is how it is paid.
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), primary_key=True),
        sa.Column("billing_cycle", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("starter_pack_used", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_addon_packs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("razorpay_customer_id", sa.String(40)),
        sa.Column("razorpay_subscription_id", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("billing_cycle IN ('starter', 'monthly', 'yearly')", name="subscriptions_billing_cycle"),
        sa.CheckConstraint("status IN ('active', 'past_due', 'cancelled')", name="subscriptions_status"),
        sa.CheckConstraint("storage_addon_packs >= 0", name="subscriptions_packs_not_negative"),
        sa.CheckConstraint("current_period_end > current_period_start", name="subscriptions_period_order"),
    )

    # Clinics that already exist start on the Starter pack from the day they were created.
    op.execute(
        "INSERT INTO subscriptions (clinic_id, billing_cycle, status, starter_pack_used, "
        "current_period_start, current_period_end) "
        "SELECT id, 'starter', 'active', true, created_at, created_at + interval '3 months' FROM clinics"
    )

    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY clinic_isolation ON subscriptions "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    # A clinic can read its own subscription. Only payments and the platform change it.
    op.execute("REVOKE ALL ON subscriptions FROM denti_app")
    op.execute("GRANT SELECT ON subscriptions TO denti_app")


def downgrade() -> None:
    op.drop_table("subscriptions")
