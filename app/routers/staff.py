import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit import record
from app.deps import Current, get_current, require_admin
from app.files_service import read_file, store_file
from app.models import AuthSession, File, Staff, TrustedDevice
from app.plans import PLAN_NAMES, PLANS
from app.schemas import PersonBrief, SignatureIn, StaffCreate, StaffOut, StaffUpdate
from app.security import hash_password, now
from app.signature import SignatureError, clean_signature

# Managing logins. Owner and receptionist only; doctors cannot reach these, except their
# own signature, which every doctor may set for their prescriptions.

BIN_DAYS = 30

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("", response_model=list[StaffOut])
def list_staff(current: Current = Depends(require_admin)):
    return current.db.scalars(select(Staff).order_by(Staff.name)).all()


@router.get("/doctors", response_model=list[PersonBrief])
def list_doctors(current: Current = Depends(get_current)):
    """The people who treat patients, for doctor pickers and filters. Anyone signed in may read it."""
    rows = current.db.scalars(
        select(Staff).where(Staff.treats_patients.is_(True), Staff.status == "active").order_by(Staff.name)
    ).all()
    return [PersonBrief(id=s.id, name=s.name) for s in rows]


def _check_seat(db: Session, plan: str, role: str) -> None:
    """Refuses one more active login than the plan includes."""
    limits = PLANS[plan]
    seat_limit = limits.receptionists if role == "admin" else limits.doctors
    if seat_limit is None:
        return
    in_use = db.scalar(select(func.count()).select_from(Staff).where(Staff.role == role, Staff.status == "active"))
    if in_use >= seat_limit:
        kind = "receptionist" if role == "admin" else "doctor"
        logins = "login" if seat_limit == 1 else "logins"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Your {PLAN_NAMES[plan]} plan includes {seat_limit} {kind} {logins}. Upgrade your plan to add more.",
        )


@router.post("", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
def create_staff(payload: StaffCreate, request: Request, current: Current = Depends(require_admin)):
    db = current.db
    _check_seat(db, current.clinic.plan, payload.role)

    email = payload.email.lower()
    if db.scalar(select(Staff.id).where(func.lower(Staff.email) == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Someone at this clinic already uses this email.")

    staff = Staff(
        clinic_id=current.clinic.id,
        name=payload.name.strip(),
        email=email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=payload.role,
        treats_patients=payload.role == "doctor",
        qualification=payload.qualification,
        specialty=payload.specialty,
        registration_no=payload.registration_no,
    )
    db.add(staff)
    db.flush()
    record(
        db,
        clinic_id=current.clinic.id,
        action="create",
        actor_id=current.staff.id,
        entity_type="staff",
        entity_id=staff.id,
        changes={"name": staff.name, "email": staff.email, "role": staff.role},
        request=request,
    )
    db.commit()
    return staff


@router.patch("/{staff_id}", response_model=StaffOut)
def update_staff(staff_id: uuid.UUID, payload: StaffUpdate, request: Request, current: Current = Depends(require_admin)):
    db = current.db
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this person.")
    changes = {}
    for field, value in payload.model_dump().items():
        if getattr(staff, field) != value:
            changes[field] = [getattr(staff, field), value]
            setattr(staff, field, value)
    if changes:
        record(
            db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="staff",
            entity_id=staff.id, changes=changes, request=request,
        )
        db.commit()
    return staff


@router.post("/{staff_id}/deactivate", response_model=StaffOut)
def deactivate_staff(staff_id: uuid.UUID, request: Request, current: Current = Depends(require_admin)):
    db = current.db
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this person.")
    if staff.role == "owner":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The owner's login cannot be deactivated.")
    if staff.id == current.staff.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own login.")
    if staff.status == "deactivated":
        return staff

    before = staff.status
    staff.status = "deactivated"
    # Signs them out everywhere at once: open sessions, trusted devices and access tokens.
    staff.token_version += 1
    db.execute(update(AuthSession).where(AuthSession.staff_id == staff.id, AuthSession.revoked_at.is_(None)).values(revoked_at=now()))
    db.execute(update(TrustedDevice).where(TrustedDevice.staff_id == staff.id, TrustedDevice.revoked_at.is_(None)).values(revoked_at=now()))
    record(
        db,
        clinic_id=current.clinic.id,
        action="update",
        actor_id=current.staff.id,
        entity_type="staff",
        entity_id=staff.id,
        changes={"status": [before, "deactivated"]},
        request=request,
    )
    db.commit()
    return staff


def _signature_owner(current: Current, staff_id: uuid.UUID) -> Staff:
    """A signature is managed by its own doctor, or by the owner or receptionist."""
    if current.staff.id != staff_id and current.staff.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only change your own signature.")
    staff = current.db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this person.")
    return staff


def _bin_signature(current: Current, staff: Staff) -> None:
    """Moves the current signature to the bin, where it stays for 30 days."""
    if staff.signature_file_id is None:
        return
    old = current.db.get(File, staff.signature_file_id)
    if old is not None and old.deleted_at is None:
        old.deleted_at = now()
        old.deleted_by = current.staff.id
        old.purge_after = old.deleted_at + timedelta(days=BIN_DAYS)
    staff.signature_file_id = None


@router.put("/{staff_id}/signature", response_model=StaffOut)
def set_signature(staff_id: uuid.UUID, payload: SignatureIn, request: Request, current: Current = Depends(get_current)):
    db = current.db
    staff = _signature_owner(current, staff_id)
    if not staff.treats_patients:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only people who treat patients sign prescriptions.")
    try:
        png, width, height = clean_signature(payload.image_data)
    except SignatureError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))

    had_one = staff.signature_file_id is not None
    _bin_signature(current, staff)
    stored = store_file(
        db, clinic_id=current.clinic.id, kind="signature", data=png, content_type="image/png",
        uploaded_by=current.staff.id, width=width, height=height,
    )
    staff.signature_file_id = stored.id
    record(
        db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="staff",
        entity_id=staff.id, changes={"signature": "replaced" if had_one else "added"}, request=request,
    )
    db.commit()
    return staff


@router.delete("/{staff_id}/signature", response_model=StaffOut)
def remove_signature(staff_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    db = current.db
    staff = _signature_owner(current, staff_id)
    if staff.signature_file_id is None:
        return staff
    _bin_signature(current, staff)
    record(
        db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="staff",
        entity_id=staff.id, changes={"signature": "removed"}, request=request,
    )
    db.commit()
    return staff


@router.get("/{staff_id}/signature")
def get_signature(staff_id: uuid.UUID, current: Current = Depends(get_current)):
    staff = _signature_owner(current, staff_id)
    stored = current.db.get(File, staff.signature_file_id) if staff.signature_file_id else None
    if stored is None or stored.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No signature has been added yet.")
    return Response(content=read_file(stored), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/{staff_id}/resume", response_model=StaffOut)
def resume_staff(staff_id: uuid.UUID, request: Request, current: Current = Depends(require_admin)):
    """Switches a login paused by a move to a smaller plan back on, when the plan has room."""
    db = current.db
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this person.")
    if staff.status != "paused":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a paused login can be switched back on.")
    _check_seat(db, current.clinic.plan, staff.role)
    staff.status = "active"
    record(
        db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="staff",
        entity_id=staff.id, changes={"status": ["paused", "active"]}, request=request,
    )
    db.commit()
    return staff
