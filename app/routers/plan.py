import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record
from app.deps import Current, require_admin, require_owner
from app.files_service import storage_used
from app.models import Clinic, Patient, PlanChange, Staff, Subscription
from app.plans import (
    FEATURES,
    GST_PERCENT,
    PLAN_NAMES,
    PLANS,
    PRICES,
    STARTER_PACK_MONTHS,
    STORAGE_PACK_GB,
    STORAGE_PACK_MONTHLY,
    storage_limit_bytes,
)
from app.schemas import (
    BillingOut,
    Meter,
    PersonBrief,
    PlanChangeIn,
    PlanChangeOut,
    PlanChangePreview,
    PlanFeature,
    PlanOption,
    PlanOut,
    PlanUsage,
    SeatChoice,
)
from app.security import now
from app.subscriptions import (
    apply_change,
    apply_due_change,
    check_keep_list,
    features_between,
    is_upgrade,
    payment_method,
    period_price,
    seat_reviews,
    upgrade_charge,
)

# The clinic's plan, how much of it is used, what every plan includes, and moving to another
# plan. For the owner and receptionist to see; only the owner changes the plan. Doctors never
# see prices or billing.

router = APIRouter(prefix="/plan", tags=["plan"])


def _active_staff(db: Session, role: str) -> int:
    return db.scalar(select(func.count()).select_from(Staff).where(Staff.role == role, Staff.status == "active"))


def _patients(db: Session) -> int:
    # Every patient ever registered counts, merged duplicates and imports included.
    return db.scalar(select(func.count()).select_from(Patient))


def _pending(db: Session) -> PlanChange | None:
    return db.scalar(select(PlanChange).where(PlanChange.status == "pending"))


def _change_out(db: Session, change: PlanChange) -> PlanChangeOut:
    person = db.get(Staff, change.requested_by)
    return PlanChangeOut(
        id=change.id, kind=change.kind, from_plan=change.from_plan, from_plan_name=PLAN_NAMES[change.from_plan],
        to_plan=change.to_plan, to_plan_name=PLAN_NAMES[change.to_plan], status=change.status, effective_at=change.effective_at,
        amount_paise=change.amount_paise, gst_paise=change.gst_paise, total_paise=change.total_paise,
        payment_method=change.payment_method, requested_by=PersonBrief(id=person.id, name=person.name),
        requested_at=change.requested_at,
    )


def _plan_out(current: Current) -> PlanOut:
    db, clinic = current.db, current.clinic
    limits = PLANS[clinic.plan]
    subscription = db.get(Subscription, clinic.id)
    packs = subscription.storage_addon_packs if subscription else 0
    usage = PlanUsage(
        patients=Meter(used=_patients(db), limit=limits.patients),
        receptionists=Meter(used=_active_staff(db, "admin"), limit=limits.receptionists),
        doctors=Meter(used=_active_staff(db, "doctor"), limit=limits.doctors),
        # The same count the upload check uses: files in the bin still take space.
        storage_bytes=Meter(used=storage_used(db), limit=storage_limit_bytes(clinic.plan, packs)),
    )
    pending = _pending(db)
    return PlanOut(
        plan=clinic.plan,
        plan_name=PLAN_NAMES[clinic.plan],
        billing=BillingOut.model_validate(subscription) if subscription else None,
        usage=usage,
        plans=[
            PlanOption(
                key=key, name=PLAN_NAMES[key], receptionists=plan.receptionists, doctors=plan.doctors,
                patients=plan.patients, storage_gb=plan.storage_gb, starter_pack_paise=PRICES[key].starter_pack,
                monthly_paise=PRICES[key].monthly, yearly_paise=PRICES[key].yearly,
            )
            for key, plan in PLANS.items()
        ],
        features=[PlanFeature(label=label, plans=list(plans)) for label, plans in FEATURES],
        starter_pack_months=STARTER_PACK_MONTHS,
        storage_pack_gb=STORAGE_PACK_GB,
        storage_pack_monthly_paise=STORAGE_PACK_MONTHLY,
        gst_percent=GST_PERCENT,
        pending_change=_change_out(db, pending) if pending else None,
        can_change=current.staff.role == "owner",
    )


@router.get("", response_model=PlanOut)
def get_plan(request: Request, current: Current = Depends(require_admin)):
    apply_due_change(current.db, current.clinic, request)
    return _plan_out(current)


def _preview(db: Session, clinic: Clinic, to_plan: str) -> PlanChangePreview:
    if to_plan == clinic.plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"You are already on {PLAN_NAMES[to_plan]}.")
    subscription = db.get(Subscription, clinic.id)
    moment = now()
    upgrade = is_upgrade(clinic.plan, to_plan)
    cycle = subscription.billing_cycle if subscription else "monthly"
    next_cycle = "yearly" if cycle == "yearly" else "monthly"
    gained, lost = features_between(clinic.plan, to_plan)

    if upgrade:
        amount, gst = upgrade_charge(subscription, clinic.plan, to_plan, moment)
        effective_at, seats, patients, storage = moment, [], None, None
    else:
        amount = gst = 0
        effective_at = subscription.current_period_end if subscription and subscription.current_period_end > moment else moment
        seats = [
            SeatChoice(
                role=r.role, limit=r.limit, active=[PersonBrief(id=s.id, name=s.name) for s in r.active],
                suggested_keep=[s.id for s in r.active[: r.limit]],
            )
            for r in seat_reviews(db, to_plan)
            if r.over
        ]
        limits = PLANS[to_plan]
        used_patients = _patients(db)
        patients = Meter(used=used_patients, limit=limits.patients) if limits.patients is not None and used_patients > limits.patients else None
        packs = subscription.storage_addon_packs if subscription else 0
        storage_limit = storage_limit_bytes(to_plan, packs)
        used_storage = storage_used(db)
        storage = Meter(used=used_storage, limit=storage_limit) if used_storage > storage_limit else None

    return PlanChangePreview(
        to_plan=to_plan, to_plan_name=PLAN_NAMES[to_plan], kind="upgrade" if upgrade else "downgrade", effective_at=effective_at,
        amount_paise=amount, gst_paise=gst, total_paise=amount + gst, next_price_paise=period_price(to_plan, next_cycle),
        next_billing_cycle=next_cycle, gained=gained, lost=lost, seats=seats, patients=patients, storage_bytes=storage,
        payment_method=payment_method(),
    )


@router.get("/changes/preview", response_model=PlanChangePreview)
def preview_change(to: str = Query(pattern="^(basic|standard|pro)$"), current: Current = Depends(require_owner)):
    """What moving to another plan would cost and change, before the owner decides."""
    apply_due_change(current.db, current.clinic)
    return _preview(current.db, current.clinic, to)


def _cancel(db: Session, change: PlanChange, by: uuid.UUID) -> None:
    change.status, change.cancelled_at, change.cancelled_by = "cancelled", now(), by
    db.flush()


@router.post("/changes", response_model=PlanOut)
def change_plan(payload: PlanChangeIn, request: Request, current: Current = Depends(require_owner)):
    """Moving up applies now, once the difference is paid. Moving down is scheduled for the
    end of the paid period, and can be cancelled until then."""
    db, clinic, staff = current.db, current.clinic, current.staff
    apply_due_change(db, clinic, request)
    preview = _preview(db, clinic, payload.to_plan)
    keep = [] if preview.kind == "upgrade" else check_keep_list(db, payload.to_plan, payload.keep_staff_ids)

    method = payment_method()
    if preview.total_paise > 0 and method != "test":
        # Razorpay checkout joins here; until then a live clinic cannot be charged online.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Online payment is not set up yet. Please contact Denti support to move up a plan.",
        )

    # The newest decision replaces a move that was still waiting.
    waiting = _pending(db)
    if waiting is not None:
        _cancel(db, waiting, staff.id)
        record(
            db, clinic_id=clinic.id, action="cancel", actor_id=staff.id, entity_type="plan_change", entity_id=waiting.id,
            changes={"plan": [PLAN_NAMES[waiting.from_plan], PLAN_NAMES[waiting.to_plan]], "reason": "replaced by a new choice"},
            request=request,
        )

    change = PlanChange(
        clinic_id=clinic.id, kind=preview.kind, from_plan=clinic.plan, to_plan=payload.to_plan,
        effective_at=preview.effective_at, amount_paise=preview.amount_paise, gst_paise=preview.gst_paise,
        total_paise=preview.total_paise, keep_staff_ids=keep, requested_by=staff.id,
    )
    db.add(change)
    db.flush()
    record(
        db, clinic_id=clinic.id, action="create", actor_id=staff.id, entity_type="plan_change", entity_id=change.id,
        changes={
            "plan": [PLAN_NAMES[change.from_plan], PLAN_NAMES[change.to_plan]], "takes_effect": change.effective_at.isoformat(),
            "amount_paise": change.total_paise,
        },
        request=request,
    )
    if preview.kind == "upgrade" and change.total_paise > 0:
        change.payment_method, change.payment_ref, change.paid_at = "test", f"TEST{secrets.token_hex(6).upper()}", now()
        record(
            db, clinic_id=clinic.id, action="create", actor_id=staff.id, entity_type="plan_payment", entity_id=change.id,
            changes={"amount_paise": change.total_paise, "method": "test payment, no money taken"}, request=request,
        )
    if change.effective_at <= now():
        apply_change(db, clinic, change, request, actor_id=staff.id)
    db.commit()
    return _plan_out(current)


@router.post("/changes/{change_id}/cancel", response_model=PlanOut)
def cancel_change(change_id: uuid.UUID, request: Request, current: Current = Depends(require_owner)):
    """Keeps the current plan: a scheduled move down does not happen."""
    db, clinic = current.db, current.clinic
    change = db.get(PlanChange, change_id)
    if change is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this plan change.")
    if change.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This plan change has already happened or was cancelled.")
    _cancel(db, change, current.staff.id)
    record(
        db, clinic_id=clinic.id, action="cancel", actor_id=current.staff.id, entity_type="plan_change", entity_id=change.id,
        changes={"plan": [PLAN_NAMES[change.from_plan], PLAN_NAMES[change.to_plan]]}, request=request,
    )
    db.commit()
    return _plan_out(current)
