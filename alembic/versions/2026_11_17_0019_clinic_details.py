"""A clinic's details and letterhead: its logo, tagline, address, phone, email, website and
GSTIN, printed on prescriptions, invoices and estimates; the letterhead's design and brand colour
(Standard and Pro); a line of its own at the foot of documents; hiding "Powered by Denti" (Pro);
and printing on paper with the letterhead already printed on it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEXT_COLUMNS = [
    ("tagline", 120),
    ("address", 300),
    ("phone", 40),
    ("email", 255),
    ("website", 200),
    ("gstin", 15),
    ("footer_text", 200),
]


def upgrade() -> None:
    # Adding the logo's link and the checks reads the existing rows, which row level security
    # hides even from the table's owner; the migration role lifts FORCE for this step only.
    for table in ("clinic_settings", "files"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    for name, length in TEXT_COLUMNS:
        op.add_column("clinic_settings", sa.Column(name, sa.String(length)))
    op.add_column("clinic_settings", sa.Column("logo_file_id", postgresql.UUID(as_uuid=True)))
    # The logo is one of the clinic's own files, like a doctor's signature.
    op.create_foreign_key(
        "clinic_settings_logo_file", "clinic_settings", "files", ["clinic_id", "logo_file_id"], ["clinic_id", "id"]
    )
    op.add_column("clinic_settings", sa.Column("brand_colour", sa.String(7)))
    op.add_column(
        "clinic_settings", sa.Column("letterhead_design", sa.String(20), nullable=False, server_default="classic")
    )
    op.add_column("clinic_settings", sa.Column("hide_powered_by", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column(
        "clinic_settings", sa.Column("preprinted_letterhead", sa.Boolean, nullable=False, server_default=sa.false())
    )
    op.add_column(
        "clinic_settings", sa.Column("preprinted_top_margin_mm", sa.Integer, nullable=False, server_default="40")
    )
    op.create_check_constraint(
        "clinic_settings_letterhead_design", "clinic_settings", "letterhead_design IN ('classic', 'centred', 'band')"
    )
    op.create_check_constraint(
        "clinic_settings_brand_colour", "clinic_settings", "brand_colour IS NULL OR brand_colour ~ '^#[0-9a-f]{6}$'"
    )
    op.create_check_constraint(
        "clinic_settings_top_margin", "clinic_settings", "preprinted_top_margin_mm BETWEEN 10 AND 120"
    )
    for table in ("clinic_settings", "files"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_constraint("clinic_settings_top_margin", "clinic_settings", type_="check")
    op.drop_constraint("clinic_settings_brand_colour", "clinic_settings", type_="check")
    op.drop_constraint("clinic_settings_letterhead_design", "clinic_settings", type_="check")
    op.drop_constraint("clinic_settings_logo_file", "clinic_settings", type_="foreignkey")
    for name in ("preprinted_top_margin_mm", "preprinted_letterhead", "hide_powered_by", "letterhead_design", "brand_colour", "logo_file_id"):
        op.drop_column("clinic_settings", name)
    for name, _ in reversed(TEXT_COLUMNS):
        op.drop_column("clinic_settings", name)
