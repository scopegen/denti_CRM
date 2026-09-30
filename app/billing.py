"""The patient's bill: ledger entries that are only ever added, never edited.

What a treatment adds to the bill:
- Planned: nothing.
- Started: a treatment_charge for its price, then a price_change for any increase or
  discount already set.
- Price, increase or discount changed after starting: a price_change for the difference, so
  what was billed before stays exactly as it was.
- Cancelled after starting: a price_change that takes off everything it added.
"""
import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clinic_time import clinic_today
from app.models import Clinic, LedgerEntry, Treatment
from app.security import now

_TREATMENT_KINDS = ("treatment_charge", "price_change")


def effective_at(clinic: Clinic, day: date) -> datetime:
    """When an entry counts. Today: now. An earlier day entered by hand: midday that day."""
    if day == clinic_today(clinic):
        return now()
    return datetime.combine(day, time(12, 0), tzinfo=ZoneInfo(clinic.timezone))


def _share(amount: int, basis_points: int) -> int:
    """A percentage of an amount, in whole paise, rounding half up."""
    return (amount * basis_points + 5000) // 10000


def final_price(
    price: int, adjustment_type: str | None, adjustment_value: int, discount_type: str | None, discount_value: int
) -> int:
    """The price after an increase, then a discount on the increased price. Never below zero."""
    adjusted = price
    if adjustment_type == "percent":
        adjusted += _share(price, adjustment_value)
    elif adjustment_type == "amount":
        adjusted += adjustment_value
    discount = 0
    if discount_type == "percent":
        discount = _share(adjusted, discount_value)
    elif discount_type == "amount":
        discount = discount_value
    return max(adjusted - discount, 0)


def treatment_final_price(t: Treatment) -> int:
    return final_price(t.price_paise, t.adjustment_type, t.adjustment_value, t.discount_type, t.discount_value)


def billed_for(db: Session, treatment_id: uuid.UUID) -> int:
    """What the bill holds for this treatment so far, in whole paise."""
    total = db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
            LedgerEntry.treatment_id == treatment_id, LedgerEntry.kind.in_(_TREATMENT_KINDS)
        )
    )
    # Postgres sums big integers as numeric; the bill is always whole paise.
    return int(total)


def format_inr(paise: int) -> str:
    """₹6,500 or ₹1,25,000.50, grouped the Indian way, for notes on the bill."""
    sign = "-" if paise < 0 else ""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return f"{sign}₹{digits}" + (f".{rest:02d}" if rest else "")


def describe_price(t: Treatment) -> str | None:
    """Increase 5%, discount ₹500: what changed the price from the listed one, if anything."""
    parts = []
    if t.adjustment_type and t.adjustment_value:
        value = f"{t.adjustment_value / 100:g}%" if t.adjustment_type == "percent" else format_inr(t.adjustment_value)
        parts.append(f"increase {value}")
    if t.discount_type and t.discount_value:
        value = f"{t.discount_value / 100:g}%" if t.discount_type == "percent" else format_inr(t.discount_value)
        parts.append(f"discount {value}")
    if not parts:
        return None
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def _entry(clinic: Clinic, t: Treatment, kind: str, amount: int, when: datetime, actor: uuid.UUID, note: str | None) -> LedgerEntry:
    return LedgerEntry(
        clinic_id=clinic.id, patient_id=t.patient_id, kind=kind, amount_paise=amount, treatment_id=t.id,
        effective_at=when, created_by=actor, note=note,
    )


def bill_started_treatment(db: Session, clinic: Clinic, t: Treatment, actor: uuid.UUID, day: date, note: str | None = None) -> None:
    """Brings the bill in line with a started treatment's final price. The first time, that
    is its charge; after that, only the difference is added."""
    when = effective_at(clinic, day)
    has_charge = db.scalar(
        select(LedgerEntry.id).where(LedgerEntry.treatment_id == t.id, LedgerEntry.kind == "treatment_charge")
    ) is not None
    billed = billed_for(db, t.id)
    if not has_charge:
        db.add(_entry(clinic, t, "treatment_charge", t.price_paise, when, actor, None))
        billed += t.price_paise
    difference = treatment_final_price(t) - billed
    if difference != 0:
        db.add(_entry(clinic, t, "price_change", difference, when, actor, note or describe_price(t) or "Price changed"))


def unbill_treatment(db: Session, clinic: Clinic, t: Treatment, actor: uuid.UUID, day: date) -> int:
    """Takes a cancelled treatment's charges off the bill. Returns the amount taken off."""
    billed = billed_for(db, t.id)
    if billed != 0:
        db.add(_entry(clinic, t, "price_change", -billed, effective_at(clinic, day), actor, "Treatment cancelled"))
    return billed
