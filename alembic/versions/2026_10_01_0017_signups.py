"""Clinics signing up by themselves: the owner chooses a plan, gives the clinic's and their own
details, and proves their email with a 6 digit code. Only then is the clinic created, through
create_signed_up_clinic, because the API role still cannot add clinics or subscriptions itself.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_FUNCTION = """
CREATE FUNCTION create_signed_up_clinic(p_signup uuid, p_paid boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_signup pending_signups%ROWTYPE;
  v_clinic uuid := gen_random_uuid();
BEGIN
  SELECT * INTO v_signup FROM pending_signups WHERE id = p_signup FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'sign up not found' USING ERRCODE = 'DN404';
  END IF;
  IF v_signup.verified_at IS NULL THEN
    RAISE EXCEPTION 'email not confirmed' USING ERRCODE = 'DN403';
  END IF;
  IF v_signup.clinic_id IS NOT NULL THEN
    RAISE EXCEPTION 'clinic already created' USING ERRCODE = 'DN409';
  END IF;

  INSERT INTO clinics (id, slug, name, plan, timezone, patient_id_prefix)
  VALUES (v_clinic, v_signup.slug, v_signup.clinic_name, v_signup.plan, v_signup.timezone, v_signup.patient_id_prefix);
  -- Subscriptions are protected by row level security, which applies here too.
  PERFORM set_config('app.clinic_id', v_clinic::text, true);
  INSERT INTO subscriptions (clinic_id, billing_cycle, status, starter_pack_used, current_period_start, current_period_end)
  VALUES (v_clinic, 'starter', CASE WHEN p_paid THEN 'active' ELSE 'past_due' END, true, now(), now() + interval '3 months');
  UPDATE pending_signups SET clinic_id = v_clinic WHERE id = p_signup;
  RETURN v_clinic;
END $$;
"""


def upgrade() -> None:
    # Shared by all of Denti, like clinics: a sign up belongs to no clinic until it creates one.
    op.create_table(
        "pending_signups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("clinic_name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("patient_id_prefix", sa.String(12)),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("owner_name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(20)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("owner_treats_patients", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id")),
        sa.Column("payment_method", sa.String(20)),
        sa.Column("payment_ref", sa.String(100)),
        sa.Column("amount_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("gst_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("ip", sa.String(64)),
        sa.Column("device", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("plan IN ('basic', 'standard', 'pro')", name="pending_signups_plan"),
        sa.CheckConstraint("attempts >= 0", name="pending_signups_attempts"),
    )
    op.create_index("pending_signups_email", "pending_signups", ["email", "created_at"])
    op.create_index("pending_signups_ip", "pending_signups", ["ip", "created_at"])
    op.execute(f"REVOKE ALL ON pending_signups FROM {settings.db_app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON pending_signups TO {settings.db_app_role}")

    op.execute(CREATE_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION create_signed_up_clinic(uuid, boolean) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION create_signed_up_clinic(uuid, boolean) TO {settings.db_app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION create_signed_up_clinic(uuid, boolean)")
    op.drop_table("pending_signups")
