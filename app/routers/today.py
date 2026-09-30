import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import record
from app.clinic_time import clinic_today
from app.deps import Current, get_current
from app.models import Arrival, Clinic, FollowUp, Patient, Staff
from app.patients_util import patient_code
from app.schemas import (
    ArrivalCreate,
    ArrivalOut,
    FollowUpCreate,
    FollowUpItem,
    FollowUpOut,
    PatientBrief,
    PersonBrief,
    TodayOut,
)
from app.security import now

# The Today page for walk in clinics, and follow ups (calls to make and visits to expect).
# Everyone signed in uses these. Two states only: waiting, and seen. Seen is set by the
# server when a consultation or visit is saved (mark_seen below), never by hand.

router = APIRouter(tags=["today"])


def _brief(clinic: Clinic, p: Patient) -> PatientBrief:
    return PatientBrief(
        id=p.id, code=patient_code(clinic, p.patient_number), name=p.name, phone=p.phone,
        medical_conditions=list(p.medical_conditions or []),
    )


def _follow_up_out(f: FollowUp, today) -> FollowUpOut:
    return FollowUpOut(
        id=f.id, kind=f.kind, due_on=f.due_on, due_time=f.due_time, reason=f.reason, status=f.status,
        source=f.source, created_at=f.created_at, completed_at=f.completed_at,
        overdue=f.status == "upcoming" and f.due_on < today,
    )


def _doctors(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, PersonBrief]:
    if not ids:
        return {}
    return {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_(ids)))}


def _patient_or_404(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def mark_seen(db: Session, clinic: Clinic, patient_id: uuid.UUID, *, consultation_id=None, visit_id=None) -> None:
    """Called when a consultation or visit is saved: the patient's waiting arrival today turns
    to Seen. Does nothing if they did not arrive through the Today page."""
    db.execute(
        update(Arrival)
        .where(
            Arrival.patient_id == patient_id,
            Arrival.clinic_date == clinic_today(clinic),
            Arrival.seen_at.is_(None),
            Arrival.cancelled_at.is_(None),
        )
        .values(seen_at=now(), seen_by_consultation_id=consultation_id, seen_by_visit_id=visit_id)
    )


@router.get("/today", response_model=TodayOut)
def today_page(doctor_id: uuid.UUID | None = None, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    today = clinic_today(clinic)

    arrivals_q = (
        select(Arrival, Patient)
        .join(Patient, Patient.id == Arrival.patient_id)
        .where(Arrival.clinic_date == today, Arrival.cancelled_at.is_(None))
        .order_by(Arrival.arrived_at)
    )
    if doctor_id is not None:
        arrivals_q = arrivals_q.where(Arrival.preferred_doctor_id == doctor_id)
    arrivals = db.execute(arrivals_q).all()
    doctors = _doctors(db, {a.preferred_doctor_id for a, _ in arrivals if a.preferred_doctor_id})
    arrived_patient_ids = {
        pid for pid in db.scalars(
            select(Arrival.patient_id).where(Arrival.clinic_date == today, Arrival.cancelled_at.is_(None))
        )
    }

    due = db.execute(
        select(FollowUp, Patient)
        .join(Patient, Patient.id == FollowUp.patient_id)
        .where(FollowUp.status == "upcoming", FollowUp.due_on <= today)
        .order_by(FollowUp.due_on, FollowUp.due_time.nulls_last(), Patient.name)
    ).all()

    def item(f: FollowUp, p: Patient) -> FollowUpItem:
        return FollowUpItem(
            **_follow_up_out(f, today).model_dump(), patient=_brief(clinic, p), arrived_today=p.id in arrived_patient_ids
        )

    return TodayOut(
        date=today,
        expected=[item(f, p) for f, p in due if f.kind == "visit"],
        calls=[item(f, p) for f, p in due if f.kind == "call"],
        arrived=[
            ArrivalOut(
                id=a.id, patient=_brief(clinic, p), arrived_at=a.arrived_at, seen_at=a.seen_at,
                preferred_doctor=doctors.get(a.preferred_doctor_id) if a.preferred_doctor_id else None,
            )
            for a, p in arrivals
        ],
    )


@router.post("/arrivals", response_model=ArrivalOut, status_code=status.HTTP_201_CREATED)
def mark_arrived(payload: ArrivalCreate, request: Request, current: Current = Depends(get_current)):
    """Reception's one tap when a patient walks in."""
    db, clinic = current.db, current.clinic
    today = clinic_today(clinic)
    patient = _patient_or_404(db, payload.patient_id)

    doctor = None
    if payload.preferred_doctor_id is not None:
        doctor = db.get(Staff, payload.preferred_doctor_id)
        if doctor is None or not doctor.treats_patients or doctor.status != "active":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a doctor from this clinic.")

    already_waiting = db.scalar(
        select(Arrival.id).where(
            Arrival.patient_id == patient.id,
            Arrival.clinic_date == today,
            Arrival.seen_at.is_(None),
            Arrival.cancelled_at.is_(None),
        )
    )
    if already_waiting is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{patient.name} is already waiting.")

    arrival = Arrival(
        clinic_id=clinic.id, patient_id=patient.id, clinic_date=today,
        preferred_doctor_id=doctor.id if doctor else None, marked_by=current.staff.id,
    )
    db.add(arrival)
    # The patient came in, so any visit we were expecting from them is done.
    db.execute(
        update(FollowUp)
        .where(
            FollowUp.patient_id == patient.id,
            FollowUp.kind == "visit",
            FollowUp.status == "upcoming",
            FollowUp.due_on <= today,
        )
        .values(status="done", completed_at=now(), completed_by=current.staff.id)
    )
    db.flush()
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="arrival",
        entity_id=arrival.id, patient_id=patient.id, request=request,
    )
    db.commit()
    return ArrivalOut(
        id=arrival.id, patient=_brief(clinic, patient), arrived_at=arrival.arrived_at, seen_at=None,
        preferred_doctor=PersonBrief(id=doctor.id, name=doctor.name) if doctor else None,
    )


@router.post("/arrivals/{arrival_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_arrival(arrival_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    """For a patient marked as arrived by mistake. The row is kept, marked cancelled."""
    db = current.db
    arrival = db.get(Arrival, arrival_id)
    if arrival is None or arrival.cancelled_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this arrival.")
    if arrival.seen_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This patient has already been seen.")
    arrival.cancelled_at = now()
    arrival.cancelled_by = current.staff.id
    record(
        db, clinic_id=current.clinic.id, action="cancel", actor_id=current.staff.id, entity_type="arrival",
        entity_id=arrival.id, patient_id=arrival.patient_id, request=request,
    )
    db.commit()


@router.get("/patients/{patient_id}/follow-ups", response_model=list[FollowUpOut])
def list_follow_ups(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    db = current.db
    _patient_or_404(db, patient_id)
    today = clinic_today(current.clinic)
    rows = db.scalars(
        select(FollowUp).where(FollowUp.patient_id == patient_id).order_by(FollowUp.due_on.desc(), FollowUp.created_at.desc())
    ).all()
    return [_follow_up_out(f, today) for f in rows]


@router.post("/patients/{patient_id}/follow-ups", response_model=FollowUpOut, status_code=status.HTTP_201_CREATED)
def add_follow_up(patient_id: uuid.UUID, payload: FollowUpCreate, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    patient = _patient_or_404(db, patient_id)
    today = clinic_today(clinic)
    if payload.due_on < today:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Choose today or a later date.")
    follow_up = FollowUp(
        clinic_id=clinic.id, patient_id=patient.id, kind=payload.kind, due_on=payload.due_on,
        due_time=payload.due_time, reason=payload.reason, created_by=current.staff.id,
    )
    db.add(follow_up)
    db.flush()
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="follow_up",
        entity_id=follow_up.id, patient_id=patient.id,
        changes={"kind": payload.kind, "due_on": payload.due_on.isoformat(), "reason": payload.reason}, request=request,
    )
    db.commit()
    db.refresh(follow_up)
    return _follow_up_out(follow_up, today)


def _close_follow_up(follow_up_id: uuid.UUID, new_status: str, request: Request, current: Current) -> FollowUpOut:
    db = current.db
    follow_up = db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this follow up.")
    if follow_up.status != "upcoming":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This follow up is already closed.")
    follow_up.status = new_status
    follow_up.completed_at = now()
    follow_up.completed_by = current.staff.id
    record(
        db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="follow_up",
        entity_id=follow_up.id, patient_id=follow_up.patient_id, changes={"status": ["upcoming", new_status]},
        request=request,
    )
    db.commit()
    return _follow_up_out(follow_up, clinic_today(current.clinic))


@router.post("/follow-ups/{follow_up_id}/done", response_model=FollowUpOut)
def follow_up_done(follow_up_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    return _close_follow_up(follow_up_id, "done", request, current)


@router.post("/follow-ups/{follow_up_id}/cancel", response_model=FollowUpOut)
def follow_up_cancel(follow_up_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    return _close_follow_up(follow_up_id, "cancelled", request, current)
