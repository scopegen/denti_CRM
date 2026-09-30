"""Give every clinic the starter lists it is missing. A clinic created before a list existed
(the demo clinic had no payment modes) could not take a payment, because the Take payment
window had no mode to choose.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The starter lists as they were when this ran (app/defaults.py may change later).
LISTS: dict[str, list[str]] = {
    "payment_mode": ["Cash", "Card", "UPI"],
    "medical_condition": [
        "Diabetes",
        "Hypertension (High Blood Pressure)",
        "Heart Disease",
        "Bleeding Disorder / Blood Thinners",
        "Asthma",
        "Epilepsy / Seizure Disorder",
        "Thyroid Disorder",
        "Pregnancy",
        "Allergy to Medication (e.g. Penicillin)",
        "Smoking / Tobacco Use",
    ],
}


def upgrade() -> None:
    # Only lists a clinic has no values in at all; a clinic's own edits are left alone. The
    # migration role lifts FORCE for this step only.
    op.execute("ALTER TABLE picklists NO FORCE ROW LEVEL SECURITY")
    for name, values in LISTS.items():
        rows = ", ".join("('" + value.replace("'", "''") + f"', {i})" for i, value in enumerate(values))
        op.execute(
            "INSERT INTO picklists (id, clinic_id, list, value, sort_order) "
            f"SELECT gen_random_uuid(), c.id, '{name}', v.value, v.sort_order "
            f"FROM clinics c, (VALUES {rows}) AS v(value, sort_order) "
            f"WHERE NOT EXISTS (SELECT 1 FROM picklists p WHERE p.clinic_id = c.id AND p.list = '{name}')"
        )
    op.execute("ALTER TABLE picklists FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Values added here cannot be told apart from ones a clinic added itself, so they stay.
    pass
