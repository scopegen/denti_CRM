"""Changing plan. Moving up takes effect once the price difference for the rest of the period
is paid; moving down takes effect when the paid period ends. The API role still cannot touch
clinics.plan or subscriptions: a change is applied only through apply_plan_change, which
checks it was paid or is due.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLY_FUNCTION = """
CREATE FUNCTION apply_plan_change(p_change uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_change plan_changes%ROWTYPE;
BEGIN
  -- Runs as the table owner; row level security still applies (FORCE), so only the clinic
  -- set for this transaction is visible.
  SELECT * INTO v_change FROM plan_changes WHERE id = p_change FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'plan change not found' USING ERRCODE = 'DN404';
  END IF;
  IF v_change.status <> 'pending' THEN
    RAISE EXCEPTION 'plan change is not pending' USING ERRCODE = 'DN409';
  END IF;
  IF v_change.kind = 'upgrade' AND v_change.total_paise > 0 AND v_change.paid_at IS NULL THEN
    RAISE EXCEPTION 'upgrade is not paid' USING ERRCODE = 'DN402';
  END IF;
  IF v_change.kind = 'downgrade' AND v_change.effective_at > now() THEN
    RAISE EXCEPTION 'downgrade is not due yet' USING ERRCODE = 'DN425';
  END IF;

  UPDATE clinics SET plan = v_change.to_plan WHERE id = v_change.clinic_id;
  IF v_change.kind = 'downgrade' THEN
    -- The lower plan starts a new period where the paid one ended. A Starter pack moves on to
    -- monthly billing.
    UPDATE subscriptions SET
      current_period_start = current_period_end,
      current_period_end = current_period_end
        + CASE WHEN billing_cycle = 'yearly' THEN interval '1 year' ELSE interval '1 month' END,
      billing_cycle = CASE WHEN billing_cycle = 'starter' THEN 'monthly' ELSE billing_cycle END,
      updated_at = now()
    WHERE clinic_id = v_change.clinic_id AND current_period_end <= now();
  END IF;
  UPDATE plan_changes SET status = 'applied', applied_at = now() WHERE id = p_change;
  RETURN v_change.to_plan;
END $$;
"""


def upgrade() -> None:
    op.create_table(
        "plan_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("from_plan", sa.String(20), nullable=False),
        sa.Column("to_plan", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        # Moving up: the price difference for the rest of the period, and its GST.
        sa.Column("amount_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("gst_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("payment_method", sa.String(20)),
        sa.Column("payment_ref", sa.String(100)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        # Moving down to fewer logins: who stays active. Everyone else is paused.
        sa.Column("keep_staff_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by", postgresql.UUID(as_uuid=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "requested_by"], ["staff.clinic_id", "staff.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
        sa.CheckConstraint("kind IN ('upgrade', 'downgrade')", name="plan_changes_kind"),
        sa.CheckConstraint("status IN ('pending', 'applied', 'cancelled')", name="plan_changes_status"),
        sa.CheckConstraint("from_plan IN ('basic', 'standard', 'pro') AND to_plan IN ('basic', 'standard', 'pro')", name="plan_changes_plans"),
        sa.CheckConstraint("from_plan <> to_plan", name="plan_changes_different"),
        sa.CheckConstraint(
            "amount_paise >= 0 AND gst_paise >= 0 AND total_paise = amount_paise + gst_paise", name="plan_changes_amounts"
        ),
        sa.CheckConstraint("kind = 'upgrade' OR total_paise = 0", name="plan_changes_only_upgrades_charge"),
        sa.CheckConstraint("(status = 'applied') = (applied_at IS NOT NULL)", name="plan_changes_applied_at"),
        sa.CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="plan_changes_cancelled_at"),
    )
    # One change waits at a time.
    op.create_index(
        "plan_changes_one_pending", "plan_changes", ["clinic_id"], unique=True, postgresql_where=sa.text("status = 'pending'")
    )
    op.execute("ALTER TABLE plan_changes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_changes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY clinic_isolation ON plan_changes "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    op.execute(f"REVOKE ALL ON plan_changes FROM {settings.db_app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON plan_changes TO {settings.db_app_role}")

    op.execute(APPLY_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION apply_plan_change(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION apply_plan_change(uuid) TO {settings.db_app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION apply_plan_change(uuid)")
    op.drop_table("plan_changes")
