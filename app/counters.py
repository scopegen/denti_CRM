import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Counter


def take_next(db: Session, clinic_id: uuid.UUID, kind: str, period: str = "all") -> int:
    """Returns the next number for this clinic and moves the counter on. The row lock makes
    two people registering patients at the same moment wait for each other, so numbers are
    never repeated or skipped. Runs inside the caller's transaction."""
    db.execute(
        insert(Counter)
        .values(clinic_id=clinic_id, kind=kind, period=period, next_value=1)
        .on_conflict_do_nothing(index_elements=["clinic_id", "kind", "period"])
    )
    counter = db.execute(
        select(Counter)
        .where(Counter.clinic_id == clinic_id, Counter.kind == kind, Counter.period == period)
        .with_for_update()
    ).scalar_one()
    value = counter.next_value
    counter.next_value = value + 1
    return value
