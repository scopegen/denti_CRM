import hmac
import math
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit import record, request_device, request_ip
from app.config import settings
from app.db import get_db, use_clinic
from app.deps import Current, get_current
from app.email import send_login_code, send_password_changed, send_password_reset_code
from app.ids import uuid7
from app.models import AuthSession, Clinic, LoginCode, Staff, TrustedDevice
from app.schemas import (
    ClinicOut,
    ClinicPublicOut,
    ClinicRequest,
    LoginRequest,
    LoginResponse,
    MeOut,
    PasswordChange,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetStarted,
    ResendRequest,
    SessionOut,
    StaffOut,
    VerifyRequest,
)
from app.security import (
    AccessClaims,
    burn_password_check,
    create_access_token,
    hash_login_code,
    hash_password,
    hash_token,
    mask_email,
    new_login_code,
    new_token,
    now,
    verify_password,
)
from app.subscriptions import apply_due_change

# Signing in is two steps: email and password, then a 6 digit code sent by email.
# A browser the person chose to trust skips the code for 30 days.
# A forgotten password is reset the same way: a 6 digit code by email, then a new password.

router = APIRouter(prefix="/auth", tags=["auth"])


# Every clinic lives under its own path (denti.in/smile-dental/...), so one browser can be
# signed in to two clinics. Each clinic therefore gets its own cookies, named after it.
def refresh_cookie_name(clinic_slug: str) -> str:
    return f"denti_refresh_{clinic_slug}"


def device_cookie_name(clinic_slug: str) -> str:
    return f"denti_device_{clinic_slug}"


def _cookie_path() -> str:
    return f"{settings.api_path_prefix}/auth"


def _set_cookie(response: Response, name: str, value: str, days: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=_cookie_path(),
    )


def _find_clinic(db: Session, slug: str) -> Clinic:
    clinic = db.scalar(select(Clinic).where(Clinic.slug == slug.strip().lower()))
    if clinic is None or clinic.status == "closed":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this clinic.")
    if clinic.status == "frozen":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This clinic's account is paused. The clinic owner can contact Denti support.",
        )
    return clinic


def _session_out(clinic: Clinic, staff: Staff) -> SessionOut:
    token = create_access_token(
        AccessClaims(staff_id=staff.id, clinic_id=clinic.id, role=staff.role, token_version=staff.token_version)
    )
    return SessionOut(
        access_token=token, staff=StaffOut.model_validate(staff), clinic=ClinicOut.model_validate(clinic)
    )


def _is_trusted_device(db: Session, staff: Staff, device_token: str | None) -> bool:
    if not device_token:
        return False
    device = db.scalar(
        select(TrustedDevice).where(
            TrustedDevice.device_token_hash == hash_token(device_token),
            TrustedDevice.staff_id == staff.id,
            TrustedDevice.revoked_at.is_(None),
            TrustedDevice.expires_at > now(),
        )
    )
    return device is not None


def _too_many_codes(db: Session, staff: Staff) -> bool:
    """Sign in and reset codes share one limit, so neither can be used to flood an inbox."""
    recent = db.scalar(
        select(func.count())
        .select_from(LoginCode)
        .where(LoginCode.staff_id == staff.id, LoginCode.created_at > now() - timedelta(minutes=15))
    )
    return recent >= settings.login_codes_per_15_minutes


def _send_code(db: Session, clinic: Clinic, staff: Staff, request: Request, purpose: str = "sign_in") -> LoginCode:
    if _too_many_codes(db, staff):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many codes were requested. Please wait 15 minutes and try again.",
        )

    # Only the newest code for the same purpose works.
    db.execute(
        update(LoginCode)
        .where(
            LoginCode.staff_id == staff.id,
            LoginCode.purpose == purpose,
            LoginCode.used_at.is_(None),
            LoginCode.cancelled_at.is_(None),
        )
        .values(cancelled_at=now())
    )
    code = new_login_code()
    row = LoginCode(
        id=uuid7(),
        clinic_id=clinic.id,
        staff_id=staff.id,
        purpose=purpose,
        sent_to=staff.email,
        expires_at=now() + timedelta(minutes=settings.login_code_minutes),
        ip=request_ip(request),
        device=request_device(request),
    )
    row.code_hash = hash_login_code(row.id, code)
    db.add(row)
    record(
        db,
        clinic_id=clinic.id,
        action=f"{purpose}_code_sent",
        actor_id=staff.id,
        entity_type="staff",
        entity_id=staff.id,
        request=request,
    )
    db.commit()
    if purpose == "password_reset":
        send_password_reset_code(staff.email, clinic.name, code)
    else:
        send_login_code(staff.email, clinic.name, code)
    return row


def _check_code(
    db: Session, clinic: Clinic, challenge_id: uuid.UUID, typed: str, purpose: str, request: Request
) -> tuple[LoginCode, Staff]:
    """The code for this challenge, if it is still good and matches what was typed. Five wrong
    tries cancel it."""
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="This code is no longer valid. Please request a new one."
    )
    code = db.get(LoginCode, challenge_id)
    if (
        code is None
        or code.purpose != purpose
        or code.used_at is not None
        or code.cancelled_at is not None
        or code.expires_at <= now()
    ):
        raise invalid
    staff = db.get(Staff, code.staff_id)
    if staff is None or staff.status != "active":
        raise invalid

    if not hmac.compare_digest(code.code_hash, hash_login_code(code.id, typed)):
        code.attempts += 1
        tries_left = settings.login_code_max_attempts - code.attempts
        if tries_left <= 0:
            code.cancelled_at = now()
        record(
            db,
            clinic_id=clinic.id,
            action=f"{purpose}_code_failed",
            actor_id=staff.id,
            entity_type="staff",
            entity_id=staff.id,
            request=request,
        )
        db.commit()
        if tries_left <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Too many wrong codes. Please request a new one."
            )
        tries = "try" if tries_left == 1 else "tries"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"That code is not right. {tries_left} {tries} left."
        )
    return code, staff


def _sign_out_everywhere(db: Session, staff: Staff, *, keep_session: str | None = None, forget_devices: bool) -> None:
    """End the person's sessions (all but the one named) and, if asked, forget trusted devices.
    Bumping the token version also stops access tokens already handed out."""
    staff.token_version += 1
    sessions = update(AuthSession).where(AuthSession.staff_id == staff.id, AuthSession.revoked_at.is_(None))
    if keep_session:
        sessions = sessions.where(AuthSession.refresh_token_hash != hash_token(keep_session))
    db.execute(sessions.values(revoked_at=now()))
    if forget_devices:
        db.execute(
            update(TrustedDevice)
            .where(TrustedDevice.staff_id == staff.id, TrustedDevice.revoked_at.is_(None))
            .values(revoked_at=now())
        )


def _start_session(
    db: Session,
    clinic: Clinic,
    staff: Staff,
    request: Request,
    response: Response,
    *,
    method: str,
    trust_device: bool = False,
) -> SessionOut:
    refresh_token = new_token()
    db.add(
        AuthSession(
            clinic_id=clinic.id,
            staff_id=staff.id,
            refresh_token_hash=hash_token(refresh_token),
            device=request_device(request),
            ip=request_ip(request),
            last_used_at=now(),
            expires_at=now() + timedelta(days=settings.refresh_token_days),
        )
    )
    if trust_device:
        device_token = new_token()
        db.add(
            TrustedDevice(
                clinic_id=clinic.id,
                staff_id=staff.id,
                device_token_hash=hash_token(device_token),
                device_name=request_device(request),
                expires_at=now() + timedelta(days=settings.trusted_device_days),
            )
        )
        _set_cookie(response, device_cookie_name(clinic.slug), device_token, settings.trusted_device_days)
    record(
        db,
        clinic_id=clinic.id,
        action="sign_in",
        actor_id=staff.id,
        entity_type="staff",
        entity_id=staff.id,
        changes={"method": method, "trusted_this_device": trust_device},
        request=request,
    )
    db.commit()
    _set_cookie(response, refresh_cookie_name(clinic.slug), refresh_token, settings.refresh_token_days)
    return _session_out(clinic, staff)


@router.get("/clinic/{slug}", response_model=ClinicPublicOut)
def clinic_public(slug: str, db: Session = Depends(get_db)):
    """The clinic's name for its sign in page. Nothing private."""
    return _find_clinic(db, slug)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    clinic = _find_clinic(db, payload.clinic)
    use_clinic(db, clinic.id)
    staff = db.scalar(select(Staff).where(func.lower(Staff.email) == payload.email.lower()))

    if staff is None:
        burn_password_check(payload.password)
    if staff is None or staff.status == "deactivated" or not verify_password(payload.password, staff.password_hash):
        record(
            db,
            clinic_id=clinic.id,
            action="sign_in_failed",
            actor_id=staff.id if staff else None,
            changes={"email": payload.email},
            request=request,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password.")
    # A move to a lower plan whose date has come may pause this very login.
    apply_due_change(db, clinic, request)
    if staff.status == "paused":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your login is paused because the clinic's plan has fewer logins. Please ask the clinic owner.",
        )

    if _is_trusted_device(db, staff, request.cookies.get(device_cookie_name(clinic.slug))):
        session = _start_session(db, clinic, staff, request, response, method="trusted_device")
        return LoginResponse(status="signed_in", session=session)

    code = _send_code(db, clinic, staff, request)
    return LoginResponse(
        status="code_sent",
        challenge_id=code.id,
        sent_to=mask_email(staff.email),
        resend_after_seconds=settings.login_code_resend_seconds,
    )


@router.post("/verify", response_model=SessionOut)
def verify(payload: VerifyRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    clinic = _find_clinic(db, payload.clinic)
    use_clinic(db, clinic.id)
    code, staff = _check_code(db, clinic, payload.challenge_id, payload.code, "sign_in", request)
    code.used_at = now()
    return _start_session(db, clinic, staff, request, response, method="email_code", trust_device=payload.trust_device)


@router.post("/resend", response_model=LoginResponse)
def resend(payload: ResendRequest, request: Request, db: Session = Depends(get_db)):
    clinic = _find_clinic(db, payload.clinic)
    use_clinic(db, clinic.id)
    code = db.get(LoginCode, payload.challenge_id)
    if code is None or code.purpose != "sign_in" or code.used_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Please sign in again.")

    wait = settings.login_code_resend_seconds - (now() - code.created_at).total_seconds()
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {math.ceil(wait)} seconds before asking for a new code.",
        )
    staff = db.get(Staff, code.staff_id)
    if staff is None or staff.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Please sign in again.")

    new_code = _send_code(db, clinic, staff, request)
    return LoginResponse(
        status="code_sent",
        challenge_id=new_code.id,
        sent_to=mask_email(staff.email),
        resend_after_seconds=settings.login_code_resend_seconds,
    )


@router.post("/password-reset", response_model=PasswordResetStarted)
def start_password_reset(payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db)):
    """Emails a 6 digit code to set a new password. The answer is the same whether or not the
    email has a login here, so the page cannot be used to find out who works at the clinic."""
    clinic = _find_clinic(db, payload.clinic)
    use_clinic(db, clinic.id)
    staff = db.scalar(select(Staff).where(func.lower(Staff.email) == payload.email.lower()))
    challenge_id = uuid7()
    if staff is not None and staff.status == "active":
        if _too_many_codes(db, staff):
            # Say nothing different; the newest code already sent still works.
            newest = db.scalar(
                select(LoginCode)
                .where(
                    LoginCode.staff_id == staff.id,
                    LoginCode.purpose == "password_reset",
                    LoginCode.used_at.is_(None),
                    LoginCode.cancelled_at.is_(None),
                    LoginCode.expires_at > now(),
                )
                .order_by(LoginCode.created_at.desc())
                .limit(1)
            )
            if newest is not None:
                challenge_id = newest.id
        else:
            challenge_id = _send_code(db, clinic, staff, request, purpose="password_reset").id
    return PasswordResetStarted(challenge_id=challenge_id, resend_after_seconds=settings.login_code_resend_seconds)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
def finish_password_reset(payload: PasswordResetConfirm, request: Request, db: Session = Depends(get_db)):
    """Sets the new password and signs the person out everywhere, trusted devices included,
    so whoever may have known the old password is locked out."""
    clinic = _find_clinic(db, payload.clinic)
    use_clinic(db, clinic.id)
    code, staff = _check_code(db, clinic, payload.challenge_id, payload.code, "password_reset", request)
    code.used_at = now()
    staff.password_hash = hash_password(payload.new_password)
    _sign_out_everywhere(db, staff, forget_devices=True)
    record(
        db,
        clinic_id=clinic.id,
        action="password_reset",
        actor_id=staff.id,
        entity_type="staff",
        entity_id=staff.id,
        changes={"signed_out_everywhere": True},
        request=request,
    )
    db.commit()
    send_password_changed(staff.email, clinic.name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password", response_model=SessionOut)
def change_password(payload: PasswordChange, request: Request, current: Current = Depends(get_current)):
    """A signed in person changes their own password. Their other devices are signed out and
    trusted devices forgotten; this one stays signed in with a fresh token."""
    db, clinic, staff = current.db, current.clinic, current.staff
    if not verify_password(payload.current_password, staff.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your current password is not right.")
    if verify_password(payload.new_password, staff.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The new password is the same as the old one.")
    staff.password_hash = hash_password(payload.new_password)
    _sign_out_everywhere(db, staff, keep_session=request.cookies.get(refresh_cookie_name(clinic.slug)), forget_devices=True)
    record(
        db,
        clinic_id=clinic.id,
        action="password_changed",
        actor_id=staff.id,
        entity_type="staff",
        entity_id=staff.id,
        changes={"other_devices_signed_out": True},
        request=request,
    )
    db.commit()
    send_password_changed(staff.email, clinic.name)
    return _session_out(clinic, staff)


@router.post("/refresh", response_model=SessionOut)
def refresh(payload: ClinicRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Called when the app opens or the short lived access token runs out."""
    signed_out = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in again.")
    clinic = db.scalar(select(Clinic).where(Clinic.slug == payload.clinic.strip().lower()))
    token = request.cookies.get(refresh_cookie_name(clinic.slug)) if clinic else None
    if clinic is None or clinic.status in ("frozen", "closed") or not token:
        raise signed_out

    use_clinic(db, clinic.id)
    session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == hash_token(token)))
    if session is None or session.revoked_at is not None or session.expires_at <= now():
        raise signed_out
    # Opening the app is when a move to a lower plan whose date has come takes effect. It may
    # pause this login.
    apply_due_change(db, clinic, request)
    staff = db.get(Staff, session.staff_id)
    if staff is None or staff.status != "active":
        session.revoked_at = now()
        db.commit()
        raise signed_out

    # Each use extends the session, so people who use Denti daily stay signed in.
    session.last_used_at = now()
    session.expires_at = now() + timedelta(days=settings.refresh_token_days)
    db.commit()
    _set_cookie(response, refresh_cookie_name(clinic.slug), token, settings.refresh_token_days)
    return _session_out(clinic, staff)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: ClinicRequest, request: Request, db: Session = Depends(get_db)):
    slug = payload.clinic.strip().lower()
    clinic = db.scalar(select(Clinic).where(Clinic.slug == slug))
    token = request.cookies.get(refresh_cookie_name(slug))
    if clinic is not None and token:
        use_clinic(db, clinic.id)
        session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == hash_token(token)))
        if session is not None and session.revoked_at is None:
            session.revoked_at = now()
            record(
                db,
                clinic_id=clinic.id,
                action="sign_out",
                actor_id=session.staff_id,
                entity_type="staff",
                entity_id=session.staff_id,
                request=request,
            )
            db.commit()
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    result.delete_cookie(refresh_cookie_name(slug), path=_cookie_path())
    return result


@router.get("/me", response_model=MeOut)
def me(current: Current = Depends(get_current)):
    return MeOut(staff=StaffOut.model_validate(current.staff), clinic=ClinicOut.model_validate(current.clinic))
