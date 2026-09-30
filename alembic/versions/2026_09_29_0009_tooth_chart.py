"""The tooth chart (Standard and Pro): problems found on teeth, full mouth checks, and which
services remove a tooth. The chart's colours are worked out from these and from treatments,
never stored.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# FDI: adult teeth 11 to 48, milk teeth 51 to 85.
VALID_TOOTH = (
    "(tooth BETWEEN 11 AND 48 AND tooth % 10 BETWEEN 1 AND 8 AND tooth / 10 BETWEEN 1 AND 4) OR "
    "(tooth BETWEEN 51 AND 85 AND tooth % 10 BETWEEN 1 AND 5 AND tooth / 10 BETWEEN 5 AND 8)"
)

TOOTH_PROBLEMS = ["Cavity", "Deep cavity", "Broken", "Needs root canal", "Loose", "Gum problem", "Sensitive", "Impacted", "Old filling"]


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
    op.execute(f"REVOKE ALL ON {table} FROM denti_app")
    op.execute(f"GRANT {grants} ON {table} TO denti_app")


def upgrade() -> None:
    op.create_table(
        "tooth_findings",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        sa.Column("tooth", sa.SmallInteger, nullable=False),
        # A value from the clinic's tooth_problem list, or 'missing'.
        sa.Column("problem", sa.String(120), nullable=False),
        # The consultation it was found at, if any.
        _uuid("consultation_id"),
        _uuid("noted_by", nullable=False),
        sa.Column("noted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Treated: a finished treatment on the tooth fixed it. Cleared: marked by hand as no
        # longer a problem, or entered by mistake. Either way the row stays, as history.
        sa.Column("resolution", sa.String(10)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        _uuid("resolved_by"),
        _uuid("resolved_by_treatment_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        sa.ForeignKeyConstraint(["clinic_id", "resolved_by_treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        _staff_fk("noted_by"),
        _staff_fk("resolved_by"),
        sa.CheckConstraint(VALID_TOOTH, name="tooth_findings_valid_tooth"),
        sa.CheckConstraint("resolution IS NULL OR resolution IN ('treated', 'cleared')", name="tooth_findings_resolution"),
        sa.CheckConstraint("(resolution IS NULL) = (resolved_at IS NULL)", name="tooth_findings_resolved_at"),
        sa.CheckConstraint("resolution <> 'treated' OR resolved_by_treatment_id IS NOT NULL", name="tooth_findings_treated_by"),
    )
    op.create_index("tooth_findings_patient", "tooth_findings", ["clinic_id", "patient_id", "tooth"])
    _protect("tooth_findings", "SELECT, INSERT, UPDATE")

    op.create_table(
        "mouth_checks",
        _uuid("id", nullable=False),
        _clinic(),
        _uuid("patient_id", nullable=False),
        sa.Column("checked_on", sa.Date, nullable=False),
        _uuid("checked_by", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        _staff_fk("checked_by"),
    )
    op.create_index("mouth_checks_patient", "mouth_checks", ["clinic_id", "patient_id", "checked_on"])
    _protect("mouth_checks", "SELECT, INSERT")

    op.add_column("services", sa.Column("removes_tooth", sa.Boolean, nullable=False, server_default=sa.false()))

    # Existing clinics: mark the starter extractions, and add the starter tooth problems to
    # clinics that have none. The migration role lifts FORCE for these two steps only.
    for table in ("services", "picklists"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE services SET removes_tooth = true WHERE lower(name) IN ('extraction', 'wisdom tooth removal')")
    values = ", ".join(f"('{p}', {i})" for i, p in enumerate(TOOTH_PROBLEMS))
    op.execute(
        "INSERT INTO picklists (id, clinic_id, list, value, sort_order) "
        f"SELECT gen_random_uuid(), c.id, 'tooth_problem', v.value, v.sort_order FROM clinics c, (VALUES {values}) AS v(value, sort_order) "
        "WHERE NOT EXISTS (SELECT 1 FROM picklists p WHERE p.clinic_id = c.id AND p.list = 'tooth_problem')"
    )
    for table in ("services", "picklists"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_column("services", "removes_tooth")
    op.drop_table("mouth_checks")
    op.drop_table("tooth_findings")
