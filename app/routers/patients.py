import re
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import record
from app.counters import take_next
from app.deps import Current, get_current
from app.models import Clinic, Patient, Picklist
from app.patients_util import normalise_phone, parse_patient_number, patient_code
from app.plans import PLAN_NAMES, PLANS
from app.schemas import PatientCreate, PatientIn, PatientOut, PatientPage, PatientStats, PatientSummary
from app.security import now

# Patients. Every signed in person may register, find and edit patients; doctors manage
# patients too. Patients are never deleted.

router = APIRouter(prefix="/patients", tags=["patients"])

_EDITABLE = [
    "name", "phone", "email", "gender", "dob", "birth_year", "city", "area",
    "height_cm", "weight_kg", "medical_conditions", "medical_history",
]


def _to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _out(clinic: Clinic, p: Patient) -> PatientOut:
    return PatientOut(
        id=p.id,
        patient_number=p.patient_number,
        code=patient_code(clinic, p.patient_number),
        name=p.name,
        phone=p.phone,
        email=p.email,
        gender=p.gender,
        dob=p.dob,
        birth_year=p.birth_year,
        city=p.city,
        area=p.area,
        height_cm=_to_float(p.height_cm),
        weight_kg=_to_float(p.weight_kg),
        medical_conditions=list(p.medical_conditions or []),
        medical_history=p.medical_history,
        whatsapp_consent=p.whatsapp_consent_at is not None,
        registered_at=p.created_at,
        updated_at=p.updated_at,
    )


def _summary(clinic: Clinic, p: Patient) -> PatientSummary:
    return PatientSummary(
        id=p.id,
        patient_number=p.patient_number,
        code=patient_code(clinic, p.patient_number),
        name=p.name,
        phone=p.phone,
        gender=p.gender,
        dob=p.dob,
        birth_year=p.birth_year,
        medical_conditions=list(p.medical_conditions or []),
        registered_at=p.created_at,
    )


def _check_conditions(db: Session, conditions: list[str]) -> list[str]:
    allowed = set(
        db.scalars(select(Picklist.value).where(Picklist.list == "medical_condition", Picklist.active.is_(True))).all()
    )
    unknown = [c for c in conditions if c not in allowed]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{unknown[0]} is not on this clinic's list of medical conditions.",
        )
    # Keep the clinic's order and drop repeats.
    return [c for c in dict.fromkeys(conditions)]


def _plain(value):
    """Audit log values: dates and decimals as text, so the log stays readable JSON."""
    if isinstance(value, (Decimal,)):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@router.get("", response_model=PatientPage)
def list_patients(
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current: Current = Depends(get_current),
):
    """Searches by name, phone, patient ID, or the ID from the clinic's old software. Paged on
    the server, so large clinics stay fast."""
    db = current.db
    stmt = select(Patient).where(Patient.merged_into_id.is_(None))
    if q and q.strip():
        text = q.strip()
        matches = [
            func.lower(Patient.name).contains(text.lower(), autoescape=True),
            func.lower(Patient.previous_id) == text.lower(),
        ]
        number = parse_patient_number(text)
        if number is not None:
            matches.append(Patient.patient_number == number)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 3:
            matches.append(func.replace(Patient.phone, " ", "").contains(digits, autoescape=True))
        stmt = stmt.where(or_(*matches))

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(Patient.created_at.desc(), Patient.patient_number.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PatientPage(items=[_summary(current.clinic, p) for p in rows], total=total, page=page, page_size=page_size)


@router.get("/stats", response_model=PatientStats)
def patient_stats(current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    local_now = datetime.now(ZoneInfo(clinic.timezone))
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = db.scalar(select(func.count()).select_from(Patient).where(Patient.merged_into_id.is_(None)))
    new_this_month = db.scalar(
        select(func.count()).select_from(Patient).where(Patient.merged_into_id.is_(None), Patient.created_at >= month_start)
    )
    used = db.scalar(select(func.count()).select_from(Patient))
    return PatientStats(total=total, new_this_month=new_this_month, used=used, limit=PLANS[clinic.plan].patients)


@router.post("", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
def create_patient(payload: PatientCreate, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic

    limit = PLANS[clinic.plan].patients
    if limit is not None and db.scalar(select(func.count()).select_from(Patient)) >= limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "plan_limit",
                "message": f"Your {PLAN_NAMES[clinic.plan]} plan includes {limit:,} patients. "
                "Upgrade your plan to register more.",
            },
        )

    conditions = _check_conditions(db, payload.medical_conditions)
    phone = normalise_phone(payload.phone)

    if not payload.allow_duplicate_phone:
        same_phone = db.scalars(
            select(Patient).where(Patient.phone == phone, Patient.merged_into_id.is_(None)).limit(5)
        ).all()
        if same_phone:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "duplicate_phone",
                    "message": "A patient with this phone number is already registered.",
                    "matches": [
                        {"code": patient_code(clinic, p.patient_number), "name": p.name} for p in same_phone
                    ],
                },
            )

    patient = Patient(
        clinic_id=clinic.id,
        patient_number=take_next(db, clinic.id, "patient"),
        name=payload.name,
        phone=phone,
        email=payload.email,
        gender=payload.gender,
        dob=payload.dob,
        birth_year=payload.birth_year,
        city=payload.city,
        area=payload.area,
        height_cm=payload.height_cm,
        weight_kg=payload.weight_kg,
        medical_conditions=conditions,
        medical_history=payload.medical_history,
        whatsapp_consent_at=now() if payload.whatsapp_consent else None,
        registered_by=current.staff.id,
    )
    db.add(patient)
    db.flush()
    record(
        db,
        clinic_id=clinic.id,
        action="create",
        actor_id=current.staff.id,
        entity_type="patient",
        entity_id=patient.id,
        patient_id=patient.id,
        changes={"name": patient.name, "phone": patient.phone, "number": patient.patient_number},
        request=request,
    )
    db.commit()
    return _out(clinic, patient)


@router.get("/{code}", response_model=PatientOut)
def get_patient(code: str, request: Request, current: Current = Depends(get_current)):
    """Opens a patient by the ID in the web address, such as SMILE-0042. Every opening is
    written to the audit log."""
    db = current.db
    number = parse_patient_number(code)
    patient = db.scalar(select(Patient).where(Patient.patient_number == number)) if number is not None else None
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    record(
        db,
        clinic_id=current.clinic.id,
        action="view",
        actor_id=current.staff.id,
        entity_type="patient",
        entity_id=patient.id,
        patient_id=patient.id,
        request=request,
    )
    db.commit()
    return _out(current.clinic, patient)


@router.patch("/{patient_id}", response_model=PatientOut)
def update_patient(patient_id: uuid.UUID, payload: PatientIn, request: Request, current: Current = Depends(get_current)):
    db = current.db
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")

    new_values = payload.model_dump(include=set(_EDITABLE))
    new_values["phone"] = normalise_phone(payload.phone)
    new_values["medical_conditions"] = _check_conditions(db, payload.medical_conditions)

    changes: dict[str, list] = {}
    for field in _EDITABLE:
        old, new = getattr(patient, field), new_values[field]
        if field == "medical_conditions":
            old = list(old or [])
        if isinstance(old, Decimal) or isinstance(new, Decimal):
            differs = (None if old is None else Decimal(old)) != (None if new is None else Decimal(new))
        else:
            differs = old != new
        if differs:
            changes[field] = [_plain(old), _plain(new)]
            setattr(patient, field, new)

    had_consent = patient.whatsapp_consent_at is not None
    if payload.whatsapp_consent != had_consent:
        patient.whatsapp_consent_at = now() if payload.whatsapp_consent else None
        changes["whatsapp_consent"] = [had_consent, payload.whatsapp_consent]

    if changes:
        patient.updated_at = now()
        record(
            db,
            clinic_id=current.clinic.id,
            action="update",
            actor_id=current.staff.id,
            entity_type="patient",
            entity_id=patient.id,
            patient_id=patient.id,
            changes=changes,
            request=request,
        )
        db.commit()
    return _out(current.clinic, patient)
