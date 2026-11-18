"""Every staff member's name starts each word with a capital, as new names now do: "dr meera
nair" becomes "Dr Meera Nair". Only first letters change; the rest stays as typed. Sign ups still
waiting for their email code get the same, since their owner becomes a staff member.

Documents already issued (a signed prescription keeps the doctor's name as it was) are not
touched.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.names import person_name

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _capitalise(table: str, column: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table}")).all()
    for row_id, name in rows:
        fixed = person_name(name)
        if fixed != name:
            bind.execute(sa.text(f"UPDATE {table} SET {column} = :name WHERE id = :id"), {"name": fixed, "id": row_id})


def upgrade() -> None:
    # Staff rows belong to their clinics; the migration role lifts FORCE for this step only.
    op.execute("ALTER TABLE staff NO FORCE ROW LEVEL SECURITY")
    _capitalise("staff", "name")
    op.execute("ALTER TABLE staff FORCE ROW LEVEL SECURITY")
    _capitalise("pending_signups", "owner_name")


def downgrade() -> None:
    # The names as first typed are not kept, so there is nothing to put back.
    pass
