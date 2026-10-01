import hmac
import math
import re
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record, request_device, request_ip
from app.clinic_setup import add_starter_data
from app.config import settings
from app.db import get_db, use_clinic
from app.email import send_signup_code
from app.ids import uuid7
from app.models import RESERVED_CLINIC_SLUGS, Clinic, PendingSignup
from app.plans import FEATURES, GST_PERCENT, PLAN_NAMES, PLANS, PRICES, STARTER_PACK_MONTHS
from app.routers.auth import _start_session
from app.schemas import (
    AddressCheckOut,
    PlanFeature,
    PlanOption,
    SignupDone,
    SignupIn,
    SignupPlansOut,
    SignupStarted,
    SignupVerifyIn,
)
from app.security import hash_login_code, hash_password, mask_email, new_login_code, now
from app.subscriptions import payment_method

# A clinic signs up by itself: the owner chooses a plan, gives the clinic's and their own
# details, and confirms their email with a 6 digit code. Only then is the clinic created (with
# its owner signed in), so nobody can open a clinic in someone else's name. The clinic starts on
# the Starter pack of its plan. Until online payment is connected, a live server creates it
# with the plan marked "Payment due".

router = APIRouter(prefix="/signup", tags=["sign up"])

SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
PREFIX = re.compile(r"^[A-Z0-9]{2,12}$")
SIGNUPS_PER_EMAIL_PER_HOUR = 5
SIGNUPS_PER_ADDRESS_PER_HOUR = 20


def _address_problem(db: Session, slug: str) -> str | None:
    if len(slug) < 2 or len(slug) > 40 or not SLUG.match(slug) or "--" in slug:
        return "Use small letters, numbers and single hyphens, for example smile-dental."
    if slug in RESERVED_CLINIC_SLUGS:
        return "This address is used by Denti itself. Please choose another."
    if db.scalar(select(func.count()).select_from(Clinic).where(Clinic.slug == slug)):
        return "Another clinic already uses this address. Please choose another."
    return None


def _starter_price(plan: str) -> tuple[int, int]:
    amount = PRICES[plan].starter_pack
    return amount, (amount * GST_PERCENT + 50) // 100


@router.get("/plans", response_model=SignupPlansOut)
def signup_plans():
    """The plans and what each includes, for the sign up page. Nothing private."""
    return SignupPlansOut(
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
        gst_percent=GST_PERCENT,
    )


@router.get("/address", response_model=AddressCheckOut)
def check_address(slug: str = Query(max_length=60), db: Session = Depends(get_db)):
    """Whether a clinic web address is free, as the owner types it."""
    slug = slug.strip().lower()
    problem = _address_problem(db, slug)
    return AddressCheckOut(slug=slug, available=problem is None, reason=problem)


def _too_many(db: Session, email: str, ip: str | None) -> bool:
    since = now() - timedelta(hours=1)
    by_email = db.scalar(
        select(func.count()).select_from(PendingSignup).where(PendingSignup.email == email, PendingSignup.created_at > since)
    )
    by_address = (
        db.scalar(select(func.count()).select_from(PendingSignup).where(PendingSignup.ip == ip, PendingSignup.created_at > since))
        if ip
        else 0
    )
    return by_email >= SIGNUPS_PER_EMAIL_PER_HOUR or by_address >= SIGNUPS_PER_ADDRESS_PER_HOUR


def _new_code(signup: PendingSignup) -> str:
    code = new_login_code()
    signup.code_hash = hash_login_code(signup.id, code)
    signup.attempts = 0
    signup.expires_at = now() + timedelta(minutes=settings.login_code_minutes)
    return code


def _started(signup: PendingSignup) -> SignupStarted:
    amount, gst = _starter_price(signup.plan)
    return SignupStarted(
        signup_id=signup.id, sent_to=mask_email(signup.email), resend_after_seconds=settings.login_code_resend_seconds,
        amount_paise=amount, gst_paise=gst, total_paise=amount + gst, payment_method=payment_method(),
    )


@router.post("", response_model=SignupStarted, status_code=status.HTTP_201_CREATED)
def start_signup(payload: SignupIn, request: Request, db: Session = Depends(get_db)):
    """Checks the details and emails the owner a code. No clinic exists yet."""
    problem = _address_problem(db, payload.slug)
    if problem:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=problem)
    if payload.patient_id_prefix and not PREFIX.match(payload.patient_id_prefix):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The patient ID prefix may use 2 to 12 capital letters or numbers."
        )
    ip = request_ip(request)
    if _too_many(db, payload.email, ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign ups were started. Please wait an hour and try again."
        )
    signup = PendingSignup(
        id=uuid7(), plan=payload.plan, clinic_name=payload.clinic_name, slug=payload.slug,
        patient_id_prefix=payload.patient_id_prefix, owner_name=payload.owner_name, email=payload.email, phone=payload.phone,
        password_hash=hash_password(payload.password), owner_treats_patients=payload.owner_treats_patients,
        ip=ip, device=request_device(request),
    )
    code = _new_code(signup)
    db.add(signup)
    db.commit()
    send_signup_code(signup.email, signup.clinic_name, code)
    return _started(signup)


def _open_signup(db: Session, signup_id: uuid.UUID) -> PendingSignup:
    signup = db.get(PendingSignup, signup_id)
    if signup is None or signup.clinic_id is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This sign up is finished or no longer valid. Please start again.")
    return signup


@router.post("/{signup_id}/resend", response_model=SignupStarted)
def resend_code(signup_id: uuid.UUID, db: Session = Depends(get_db)):
    signup = _open_signup(db, signup_id)
    sent_at = signup.expires_at - timedelta(minutes=settings.login_code_minutes)
    wait = settings.login_code_resend_seconds - (now() - sent_at).total_seconds()
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"Please wait {math.ceil(wait)} seconds before asking for a new code."
        )
    if _too_many(db, signup.email, None):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many codes were requested. Please wait an hour.")
    code = _new_code(signup)
    db.commit()
    send_signup_code(signup.email, signup.clinic_name, code)
    return _started(signup)


@router.post("/{signup_id}/verify", response_model=SignupDone)
def verify_and_create(signup_id: uuid.UUID, payload: SignupVerifyIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """The right code creates the clinic, takes the Starter pack payment and signs the owner in."""
    signup = _open_signup(db, signup_id)
    if signup.expires_at <= now() or signup.attempts >= settings.login_code_max_attempts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This code is no longer valid. Please ask for a new one.")
    if not hmac.compare_digest(signup.code_hash, hash_login_code(signup.id, payload.code)):
        signup.attempts += 1
        db.commit()
        left = settings.login_code_max_attempts - signup.attempts
        if left <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Too many wrong codes. Please ask for a new one.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"That code is not right. {left} {'try' if left == 1 else 'tries'} left.")

    method = payment_method()
    paid = method == "test"
    amount, gst = _starter_price(signup.plan)
    signup.verified_at = now()
    signup.amount_paise, signup.gst_paise = amount, gst
    signup.payment_method = method
    if paid:
        signup.payment_ref = f"TEST{secrets.token_hex(6).upper()}"
    db.flush()
    try:
        with db.begin_nested():
            clinic_id = db.execute(text("SELECT create_signed_up_clinic(:id, :paid)"), {"id": signup.id, "paid": paid}).scalar()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Another clinic took this web address a moment ago. Please start again with another."
        ) from None

    use_clinic(db, clinic_id)
    clinic = db.get(Clinic, clinic_id)
    owner = add_starter_data(
        db, clinic_id,
        [{"name": signup.owner_name, "email": signup.email, "phone": signup.phone, "role": "owner",
          "treats_patients": signup.owner_treats_patients}],
        signup.password_hash,
    )[0]
    # The clinic now holds the password; the sign up no longer needs it.
    signup.password_hash = "used"
    record(
        db, clinic_id=clinic_id, action="create", actor_id=owner.id, entity_type="clinic", entity_id=clinic_id,
        changes={"signed_up": True, "plan": PLAN_NAMES[signup.plan], "starter_pack_paise": amount + gst,
                 "payment": "test payment, no money taken" if paid else "payment due"},
        request=request,
    )
    session = _start_session(db, clinic, owner, request, response, method="sign_up")
    return SignupDone(session=session, clinic_slug=clinic.slug, paid=paid)
