"""The clinic's service catalog, with prices in paise.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every new clinic starts with these (name, category, kind, price in rupees, recall months).
# Migrations keep their own copy so they never change when app code changes later.
STARTER_SERVICES = [
    ("Scaling and polishing", "Cleaning", "in_house", 1500, 6),
    ("Tooth coloured filling", "Fillings", "in_house", 1500, None),
    ("Root canal treatment", "Root canal", "in_house", 6500, None),
    ("Re root canal treatment", "Root canal", "in_house", 8000, None),
    ("Extraction", "Extraction", "in_house", 1200, None),
    ("Wisdom tooth removal", "Extraction", "in_house", 4500, None),
    ("Ceramic crown", "Crowns and bridges", "lab", 9000, None),
    ("Metal ceramic crown", "Crowns and bridges", "lab", 6000, None),
    ("Complete denture", "Dentures", "lab", 15000, None),
    ("Dental implant", "Implants", "in_house", 32000, None),
    ("Teeth whitening", "Cosmetic", "in_house", 8000, None),
    ("Night guard", "Appliances", "lab", 3500, None),
]


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(80)),
        sa.Column("kind", sa.String(10), nullable=False, server_default="in_house"),
        sa.Column("listed_price_paise", sa.BigInteger, nullable=False),
        sa.Column("recall_after_months", sa.SmallInteger),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("clinic_id", "id", name="services_clinic_id_id_key"),
        sa.CheckConstraint("kind IN ('in_house', 'lab')", name="services_kind"),
        sa.CheckConstraint("listed_price_paise >= 0", name="services_price_not_negative"),
        sa.CheckConstraint(
            "recall_after_months IS NULL OR recall_after_months BETWEEN 1 AND 60", name="services_recall_months"
        ),
    )
    op.create_index("services_clinic_name", "services", ["clinic_id", sa.text("lower(name)")], unique=True)

    op.execute("ALTER TABLE services ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE services FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY clinic_isolation ON services "
        "USING (clinic_id = current_setting('app.clinic_id')::uuid) "
        "WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)"
    )
    # Services are retired (active = false), never deleted: old treatments point at them.
    op.execute("GRANT SELECT, INSERT, UPDATE ON services TO denti_app")
    op.execute("REVOKE DELETE, TRUNCATE ON services FROM denti_app")

    connection = op.get_bind()
    for (clinic_id,) in connection.execute(sa.text("SELECT id FROM clinics")).all():
        connection.execute(sa.text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        for name, category, kind, rupees, recall in STARTER_SERVICES:
            connection.execute(
                sa.text(
                    "INSERT INTO services (id, clinic_id, name, category, kind, listed_price_paise, recall_after_months) "
                    "VALUES (gen_random_uuid(), :cid, :name, :category, :kind, :paise, :recall)"
                ),
                {"cid": clinic_id, "name": name, "category": category, "kind": kind, "paise": rupees * 100, "recall": recall},
            )


def downgrade() -> None:
    op.drop_table("services")
