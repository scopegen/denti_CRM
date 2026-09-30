"""Moving between plans. Moving up takes effect at once, charging the price difference for
what is left of the paid period (plus GST). Moving down waits until the paid period ends; logins
the lower plan has no room for are then paused, never deleted, and nothing a clinic recorded is
ever removed. docs/database-design.md describes the rules."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.audit import record
from app.config import settings
from app.models import AuthSession, Clinic, PlanChange, Staff, Subscription, TrustedDevice
from app.plans import FEATURES, GST_PERCENT, PLAN_NAMES, PLANS, PRICES
from app.security import now

PLAN_ORDER = ("basic", "standard", "pro")
SEAT_ROLES = ("admin", "doctor")
ROLE_WORD = {"admin": "receptionist", "doctor": "doctor"}


def is_upgrade(from_plan: str, to_plan: str) -> bool:
    return PLAN_ORDER.index(to_plan) > PLAN_ORDER.index(from_plan)


def payment_method() -> str:
    """How an upgrade is paid. Online payment (Razorpay) is not connected yet, so outside
    production an upgrade goes through as a test payment and no money is taken."""
    if settings.payment_backend:
        return settings.payment_backend
    return "none" if settings.environment == "production" else "test"


def period_price(plan: str, billing_cycle: str) -> int:
    prices = PRICES[plan]
    return {"starter": prices.starter_pack, "monthly": prices.monthly, "yearly": prices.yearly}[billing_cycle]


def upgrade_charge(subscription: Subscription | None, from_plan: str, to_plan: str, at: datetime) -> tuple[int, int]:
    """The price difference for the rest of the paid period, to the nearest rupee, and its GST."""
    if subscription is None or subscription.current_period_end <= at:
        return 0, 0
    difference = period_price(to_plan, subscription.billing_cycle) - period_price(from_plan, subscription.billing_cycle)
    whole = int((subscription.current_period_end - subscription.current_period_start).total_seconds())
    left = int((subscription.current_period_end - max(at, subscription.current_period_start)).total_seconds())
    amount = difference * left // whole
    amount = (amount + 50) // 100 * 100
    gst = (amount * GST_PERCENT + 50) // 100
    return amount, gst


def features_between(from_plan: str, to_plan: str) -> tuple[list[str], list[str]]:
    """What the new plan adds, and what it takes away."""
    gained = [label for label, plans in FEATURES if to_plan in plans and from_plan not in plans]
    lost = [label for label, plans in FEATURES if from_plan in plans and to_plan not in plans]
    return gained, lost


@dataclass
class SeatReview:
    role: str
    limit: int | None
    active: list[Staff] = field(default_factory=list)

    @property
    def over(self) -> bool:
        return self.limit is not None and len(self.active) > self.limit


def seat_reviews(db: Session, plan: str) -> list[SeatReview]:
    """Active receptionist and doctor logins against the plan's limits, oldest first."""
    limits = PLANS[plan]
    reviews = []
    for role in SEAT_ROLES:
        active = list(
            db.scalars(select(Staff).where(Staff.role == role, Staff.status == "active").order_by(Staff.created_at, Staff.id))
        )
        reviews.append(SeatReview(role=role, limit=limits.receptionists if role == "admin" else limits.doctors, active=active))
    return reviews


def check_keep_list(db: Session, to_plan: str, keep: list[uuid.UUID]) -> list[uuid.UUID]:
    """Moving down to fewer logins: the owner must say who stays, within the new limits."""
    chosen = set(keep)
    for review in seat_reviews(db, to_plan):
        if not review.over:
            continue
        kept = [s for s in review.active if s.id in chosen]
        word = ROLE_WORD[review.role]
        if len(kept) != review.limit:
            logins = "login" if review.limit == 1 else "logins"
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{PLAN_NAMES[to_plan]} includes {review.limit} {word} {logins}. Choose {review.limit} to keep.",
            )
    return [s.id for r in seat_reviews(db, to_plan) for s in r.active if s.id in chosen]


def _sign_out(db: Session, person: Staff) -> None:
    person.token_version += 1
    db.execute(update(AuthSession).where(AuthSession.staff_id == person.id, AuthSession.revoked_at.is_(None)).values(revoked_at=now()))
    db.execute(update(TrustedDevice).where(TrustedDevice.staff_id == person.id, TrustedDevice.revoked_at.is_(None)).values(revoked_at=now()))


def pause_extra_logins(db: Session, clinic: Clinic, plan: str, keep: list[uuid.UUID], request: Request | None) -> list[Staff]:
    """Pauses the logins the plan has no room for, keeping the chosen ones first and then the
    longest serving. Paused people are signed out; everything they recorded stays."""
    paused = []
    for review in seat_reviews(db, plan):
        if not review.over:
            continue
        ordered = [s for s in review.active if s.id in keep] + [s for s in review.active if s.id not in keep]
        for person in ordered[review.limit :]:
            person.status = "paused"
            _sign_out(db, person)
            paused.append(person)
            record(
                db, clinic_id=clinic.id, action="update", actor_type="system", entity_type="staff", entity_id=person.id,
                changes={"status": ["active", "paused"], "reason": f"moved to {PLAN_NAMES[plan]}"}, request=request,
            )
    return paused


def resume_paused_logins(db: Session, clinic: Clinic, plan: str, actor_id: uuid.UUID, request: Request | None) -> list[Staff]:
    """After moving up, logins paused by an earlier move down come back, oldest first, while
    the plan has room."""
    resumed = []
    limits = PLANS[plan]
    for role in SEAT_ROLES:
        limit = limits.receptionists if role == "admin" else limits.doctors
        active = len(db.scalars(select(Staff.id).where(Staff.role == role, Staff.status == "active")).all())
        waiting = db.scalars(select(Staff).where(Staff.role == role, Staff.status == "paused").order_by(Staff.created_at, Staff.id))
        for person in waiting:
            if limit is not None and active >= limit:
                break
            person.status = "active"
            active += 1
            resumed.append(person)
            record(
                db, clinic_id=clinic.id, action="update", actor_id=actor_id, entity_type="staff", entity_id=person.id,
                changes={"status": ["paused", "active"], "reason": f"moved to {PLAN_NAMES[plan]}"}, request=request,
            )
    return resumed


def apply_change(db: Session, clinic: Clinic, change: PlanChange, request: Request | None, actor_id: uuid.UUID | None) -> None:
    """Applies a paid upgrade or a due downgrade through the database function, then pauses or
    resumes logins to fit the new plan."""
    db.flush()
    db.execute(text("SELECT apply_plan_change(:id)"), {"id": change.id})
    db.refresh(clinic)
    db.refresh(change)
    if change.kind == "downgrade":
        pause_extra_logins(db, clinic, change.to_plan, list(change.keep_staff_ids), request)
    else:
        resume_paused_logins(db, clinic, change.to_plan, actor_id, request)
    record(
        db, clinic_id=clinic.id, action="update", actor_id=actor_id, actor_type="staff" if actor_id else "system",
        entity_type="plan", entity_id=change.id,
        changes={"plan": [PLAN_NAMES[change.from_plan], PLAN_NAMES[change.to_plan]]}, request=request,
    )


def apply_due_change(db: Session, clinic: Clinic, request: Request | None = None) -> bool:
    """A move down whose date has come is applied the next time anyone at the clinic signs in,
    opens the app or looks at the plan. Returns whether the plan changed."""
    change = db.scalar(
        select(PlanChange).where(PlanChange.status == "pending", PlanChange.kind == "downgrade", PlanChange.effective_at <= now())
    )
    if change is None:
        return False
    apply_change(db, clinic, change, request, actor_id=None)
    db.commit()
    return True
