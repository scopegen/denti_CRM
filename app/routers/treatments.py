import uuid
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import bill_started_treatment, final_price, treatment_final_price, unbill_treatment
from app.clinic_time import add_months, clinic_today
from app.deps import Current, get_current, require_admin
from app.documents import date_text
from app.models import (
    Clinic,
    Consultation,
    ConsultationRecommendation,
    FollowUp,
    LedgerEntry,
    Patient,
    Prescription,
    Service,
    Staff,
    Treatment,
    TreatmentHandoff,
    Visit,
)
from app.prescriptions import latest_versions, prescription_out, save_prescription
from app.routers.today import mark_seen
from app.schemas import (
    HandoffIn,
    HandoffOut,
    PersonBrief,
    ServiceBrief,
    TreatmentCancel,
    TreatmentCreate,
    TreatmentDay,
    TreatmentMoney,
    TreatmentOut,
    TreatmentPriceIn,
    TreatmentSuggestion,
    TreatmentUpdate,
    VisitIn,
    VisitOut,
    VisitUpdate,
)
from app.security import now
from app.tooth_chart import after_treatment_finished

# Treatments and their visits. A doctor can start any of the clinic's services for a
# patient, recommended in a consultation or not. Planned treatments are not billed;
# starting one puts its charge on the bill (app/billing.py). Doctors never see prices: the
# money part of a treatment comes back empty for them, and only the owner and receptionist
# can change prices or cancel a treatment that has started.

router = APIRouter(tags=["treatments"])

_STATUS_ORDER = {"ongoing": 0, "planned": 1, "finished": 2, "cancelled": 3}


def _is_admin(staff: Staff) -> bool:
    return staff.role in ("owner", "admin")


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def _doctor(db: Session, doctor_id: uuid.UUID) -> Staff:
    doctor = db.get(Staff, doctor_id)
    if doctor is None or not doctor.treats_patients or doctor.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a doctor from this clinic.")
    return doctor


def _service(db: Session, service_id: uuid.UUID) -> Service:
    service = db.get(Service, service_id)
    if service is None or not service.active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a treatment from this clinic's services.")
    return service


def _treatment(db: Session, treatment_id: uuid.UUID) -> Treatment:
    treatment = db.get(Treatment, treatment_id)
    if treatment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this treatment.")
    return treatment


def _check_teeth_allowed(clinic: Clinic, teeth: list[int]) -> None:
    if teeth and clinic.plan == "basic":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Choosing teeth comes with the Standard and Pro plans."
        )


def _day(clinic: Clinic, value: date | None, not_before: date | None = None) -> date:
    """A date entered for something that happened: today unless given, never in the future."""
    today = clinic_today(clinic)
    day = value or today
    if day > today:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The date cannot be in the future.")
    if not_before is not None and day < not_before:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The date cannot be before the treatment started on {date_text(not_before)}.",
        )
    return day


def _plain(value):
    """Audit log values as plain JSON: ids and dates as text."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _log(db: Session, current: Current, request: Request, action: str, t: Treatment, changes: dict, entity_type="treatment", entity_id=None):
    record(
        db, clinic_id=current.clinic.id, action=action, actor_id=current.staff.id, entity_type=entity_type,
        entity_id=entity_id or t.id, patient_id=t.patient_id, changes=changes, request=request,
    )


def _start(db: Session, clinic: Clinic, t: Treatment, day: date, actor: uuid.UUID) -> None:
    t.status = "ongoing"
    t.started_on = day
    db.flush()
    bill_started_treatment(db, clinic, t, actor, day)


def _finish(db: Session, clinic: Clinic, t: Treatment, day: date, actor: uuid.UUID) -> None:
    t.status = "finished"
    t.finished_on = day
    # The tooth chart: problems on its teeth are treated; an extraction leaves them missing.
    after_treatment_finished(db, clinic, t, actor)
    # A service with a recall (a check up, a cleaning) books the call to remind the patient.
    # Automatic recalls come with the Standard and Pro plans.
    service = db.get(Service, t.service_id)
    if service.recall_after_months and clinic.plan in ("standard", "pro"):
        due = add_months(day, service.recall_after_months)
        if due >= clinic_today(clinic):
            db.add(
                FollowUp(
                    clinic_id=clinic.id, patient_id=t.patient_id, kind="call", due_on=due,
                    reason=f"Recall after {service.name}", source="recall", treatment_id=t.id, created_by=actor,
                )
            )


def _outs(db: Session, treatments: list[Treatment], viewer: Staff) -> list[TreatmentOut]:
    if not treatments:
        return []
    ids = [t.id for t in treatments]
    services = {s.id: s for s in db.scalars(select(Service).where(Service.id.in_({t.service_id for t in treatments})))}

    visits: dict[uuid.UUID, list[Visit]] = defaultdict(list)
    for v in db.scalars(select(Visit).where(Visit.treatment_id.in_(ids)).order_by(Visit.visit_date, Visit.created_at)):
        visits[v.treatment_id].append(v)
    all_visit_ids = [v.id for vs in visits.values() for v in vs]
    prescriptions = {
        p.visit_id: p for p in db.scalars(select(Prescription).where(Prescription.visit_id.in_(all_visit_ids)))
    } if all_visit_ids else {}
    versions = latest_versions(db, [p.id for p in prescriptions.values()])

    handoffs: dict[uuid.UUID, list[TreatmentHandoff]] = defaultdict(list)
    for h in db.scalars(select(TreatmentHandoff).where(TreatmentHandoff.treatment_id.in_(ids)).order_by(TreatmentHandoff.changed_at)):
        handoffs[h.treatment_id].append(h)

    people_ids = {t.doctor_id for t in treatments} | {v.doctor_id for vs in visits.values() for v in vs}
    people_ids |= {x for hs in handoffs.values() for h in hs for x in (h.from_doctor_id, h.to_doctor_id, h.changed_by)}
    people = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_(people_ids)))}

    billed: dict[uuid.UUID, int] = {}
    if _is_admin(viewer):
        billed = dict(
            db.execute(
                select(LedgerEntry.treatment_id, func.sum(LedgerEntry.amount_paise))
                .where(LedgerEntry.treatment_id.in_(ids), LedgerEntry.kind.in_(("treatment_charge", "price_change")))
                .group_by(LedgerEntry.treatment_id)
            ).all()
        )

    out = []
    for t in treatments:
        money = None
        if _is_admin(viewer):
            money = TreatmentMoney(
                price_paise=t.price_paise, adjustment_type=t.adjustment_type, adjustment_value=t.adjustment_value,
                discount_type=t.discount_type, discount_value=t.discount_value,
                final_price_paise=treatment_final_price(t), billed_paise=int(billed.get(t.id, 0)),
            )
        out.append(
            TreatmentOut(
                id=t.id, patient_id=t.patient_id,
                service=ServiceBrief(id=t.service_id, name=services[t.service_id].name),
                teeth=list(t.teeth), doctor=people[t.doctor_id], status=t.status, consultation_id=t.consultation_id,
                notes=t.notes, started_on=t.started_on, finished_on=t.finished_on, cancelled_on=t.cancelled_on,
                cancel_reason=t.cancel_reason, created_at=t.created_at,
                visits=[
                    VisitOut(
                        id=v.id, visit_date=v.visit_date, doctor=people[v.doctor_id], notes=v.notes,
                        prescription=(
                            prescription_out(db, prescriptions[v.id], versions[prescriptions[v.id].id], viewer)
                            if v.id in prescriptions else None
                        ),
                        created_at=v.created_at, updated_at=v.updated_at,
                    )
                    for v in visits[t.id]
                ],
                handoffs=[
                    HandoffOut(
                        from_doctor=people[h.from_doctor_id], to_doctor=people[h.to_doctor_id],
                        changed_by=people[h.changed_by], changed_at=h.changed_at, reason=h.reason,
                    )
                    for h in handoffs[t.id]
                ],
                money=money,
            )
        )
    return out


def _one(db: Session, t: Treatment, viewer: Staff) -> TreatmentOut:
    return _outs(db, [t], viewer)[0]


@router.get("/patients/{patient_id}/treatments", response_model=list[TreatmentOut])
def list_treatments(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    """Ongoing first, then planned, finished and cancelled; newest first within each."""
    db = current.db
    _patient(db, patient_id)
    rows = db.scalars(select(Treatment).where(Treatment.patient_id == patient_id).order_by(Treatment.created_at.desc())).all()
    rows = sorted(rows, key=lambda t: _STATUS_ORDER[t.status])
    return _outs(db, rows, current.staff)


@router.get("/patients/{patient_id}/treatments/suggestions", response_model=list[TreatmentSuggestion])
def treatment_suggestions(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    """Treatments recommended in the patient's consultations that are not treatments yet. A
    recommendation counts as taken once the patient has that treatment for the same teeth,
    however it was added: from the recommendation, from an accepted estimate, or by hand."""
    db = current.db
    _patient(db, patient_id)
    existing = list(
        db.scalars(select(Treatment).where(Treatment.patient_id == patient_id, Treatment.status != "cancelled"))
    )

    def taken(consultation_id: uuid.UUID, service_id: uuid.UUID, teeth: list[int]) -> bool:
        for t in existing:
            if t.service_id != service_id:
                continue
            if t.consultation_id == consultation_id:
                return True
            if (not teeth and not t.teeth) or set(teeth) & set(t.teeth):
                return True
        return False

    rows = db.execute(
        select(ConsultationRecommendation, Consultation, Service, Staff)
        .join(Consultation, Consultation.id == ConsultationRecommendation.consultation_id)
        .join(Service, Service.id == ConsultationRecommendation.service_id)
        .join(Staff, Staff.id == Consultation.doctor_id)
        .where(Consultation.patient_id == patient_id, Service.active.is_(True))
        .order_by(Consultation.consult_date.desc(), Consultation.created_at.desc())
    ).all()
    return [
        TreatmentSuggestion(
            consultation_id=c.id, consult_date=c.consult_date, doctor=PersonBrief(id=d.id, name=d.name),
            service=ServiceBrief(id=s.id, name=s.name), teeth=list(r.teeth),
        )
        for r, c, s, d in rows
        if not taken(c.id, s.id, list(r.teeth))
    ]


@router.post("/patients/{patient_id}/treatments", response_model=TreatmentOut, status_code=status.HTTP_201_CREATED)
def create_treatment(patient_id: uuid.UUID, payload: TreatmentCreate, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    doctor = _doctor(db, payload.doctor_id)
    service = _service(db, payload.service_id)
    _check_teeth_allowed(clinic, payload.teeth)
    if payload.price_paise is not None and not _is_admin(current.staff):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner or receptionist can set prices.")
    if payload.consultation_id is not None:
        consultation = db.get(Consultation, payload.consultation_id)
        if consultation is None or consultation.patient_id != patient.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That consultation is not this patient's.")
    start_day = _day(clinic, payload.started_on) if payload.start else None

    t = Treatment(
        clinic_id=clinic.id, patient_id=patient.id, service_id=service.id, doctor_id=doctor.id, teeth=payload.teeth,
        consultation_id=payload.consultation_id, notes=payload.notes, status="planned",
        price_paise=payload.price_paise if payload.price_paise is not None else service.listed_price_paise,
        created_by=current.staff.id,
    )
    db.add(t)
    db.flush()
    if start_day is not None:
        _start(db, clinic, t, start_day, current.staff.id)
    _log(db, current, request, "create", t, {
        "service": service.name, "teeth": t.teeth, "doctor": doctor.name, "status": t.status,
        "started_on": _plain(t.started_on), "price_paise": t.price_paise,
    })
    db.commit()
    return _one(db, t, current.staff)


@router.patch("/treatments/{treatment_id}", response_model=TreatmentOut)
def update_treatment(treatment_id: uuid.UUID, payload: TreatmentUpdate, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This treatment is cancelled.")
    changes = {}
    if payload.service_id != t.service_id:
        if t.status != "planned":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The treatment has started, so it cannot become a different treatment. Cancel it and add the right one.",
            )
        service = _service(db, payload.service_id)
        changes["service_id"] = [str(t.service_id), str(service.id)]
        changes["price_paise"] = [t.price_paise, service.listed_price_paise]
        t.service_id, t.price_paise = service.id, service.listed_price_paise
    if payload.teeth != list(t.teeth):
        _check_teeth_allowed(clinic, payload.teeth)
        changes["teeth"] = [list(t.teeth), payload.teeth]
        t.teeth = payload.teeth
    if payload.notes != t.notes:
        changes["notes"] = [t.notes, payload.notes]
        t.notes = payload.notes
    if changes:
        t.updated_at = now()
        _log(db, current, request, "update", t, changes)
        db.commit()
    return _one(db, t, current.staff)


@router.put("/treatments/{treatment_id}/price", response_model=TreatmentOut)
def set_price(treatment_id: uuid.UUID, payload: TreatmentPriceIn, request: Request, current: Current = Depends(require_admin)):
    """Price, increase and discount. On a started treatment, the bill gets the difference."""
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This treatment is cancelled.")
    fields = ["price_paise", "adjustment_type", "adjustment_value", "discount_type", "discount_value"]
    wanted = payload.model_dump()
    if payload.discount_type == "amount":
        increased = final_price(payload.price_paise, payload.adjustment_type, payload.adjustment_value, None, 0)
        if payload.discount_value > increased:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The discount is more than the price.")
    changes = {f: [getattr(t, f), wanted[f]] for f in fields if getattr(t, f) != wanted[f]}
    if changes:
        before = treatment_final_price(t)
        for f in fields:
            setattr(t, f, wanted[f])
        t.updated_at = now()
        changes["final_price_paise"] = [before, treatment_final_price(t)]
        if t.status in ("ongoing", "finished"):
            bill_started_treatment(db, clinic, t, current.staff.id, clinic_today(clinic))
        _log(db, current, request, "update", t, changes)
        db.commit()
    return _one(db, t, current.staff)


@router.post("/treatments/{treatment_id}/start", response_model=TreatmentOut)
def start_treatment(treatment_id: uuid.UUID, payload: TreatmentDay, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status != "planned":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a planned treatment can be started.")
    day = _day(clinic, payload.on)
    _start(db, clinic, t, day, current.staff.id)
    _log(db, current, request, "update", t, {"status": ["planned", "ongoing"], "started_on": day.isoformat()})
    db.commit()
    return _one(db, t, current.staff)


@router.post("/treatments/{treatment_id}/finish", response_model=TreatmentOut)
def finish_treatment(treatment_id: uuid.UUID, payload: TreatmentDay, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status != "ongoing":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a treatment in progress can be finished.")
    day = _day(clinic, payload.on, not_before=t.started_on)
    _finish(db, clinic, t, day, current.staff.id)
    _log(db, current, request, "update", t, {"status": ["ongoing", "finished"], "finished_on": day.isoformat()})
    db.commit()
    return _one(db, t, current.staff)


@router.post("/treatments/{treatment_id}/cancel", response_model=TreatmentOut)
def cancel_treatment(treatment_id: uuid.UUID, payload: TreatmentCancel, request: Request, current: Current = Depends(get_current)):
    """A planned treatment: anyone. A started one changes the bill, so only the owner or
    receptionist, and its charge comes off the bill."""
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status not in ("planned", "ongoing"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A {t.status} treatment cannot be cancelled.")
    if t.status == "ongoing" and not _is_admin(current.staff):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This treatment has started and is on the bill. Ask the owner or receptionist to cancel it.",
        )
    before = t.status
    today = clinic_today(clinic)
    taken_off = unbill_treatment(db, clinic, t, current.staff.id, today) if before == "ongoing" else 0
    t.status, t.cancelled_on, t.cancel_reason, t.updated_at = "cancelled", today, payload.reason, now()
    # Visits booked for it are no longer expected.
    db.execute(
        update(FollowUp)
        .where(FollowUp.treatment_id == t.id, FollowUp.status == "upcoming")
        .values(status="cancelled", completed_at=now(), completed_by=current.staff.id)
    )
    _log(db, current, request, "cancel", t, {"status": [before, "cancelled"], "reason": payload.reason, "taken_off_bill_paise": taken_off})
    db.commit()
    return _one(db, t, current.staff)


@router.post("/treatments/{treatment_id}/handoff", response_model=TreatmentOut)
def hand_off(treatment_id: uuid.UUID, payload: HandoffIn, request: Request, current: Current = Depends(get_current)):
    db = current.db
    t = _treatment(db, treatment_id)
    if t.status not in ("planned", "ongoing"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A {t.status} treatment cannot be handed over.")
    doctor = _doctor(db, payload.to_doctor_id)
    if doctor.id == t.doctor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{doctor.name} already has this treatment.")
    db.add(
        TreatmentHandoff(
            clinic_id=current.clinic.id, treatment_id=t.id, from_doctor_id=t.doctor_id, to_doctor_id=doctor.id,
            changed_by=current.staff.id, reason=payload.reason,
        )
    )
    _log(db, current, request, "update", t, {"doctor_id": [str(t.doctor_id), str(doctor.id)], "reason": payload.reason})
    t.doctor_id, t.updated_at = doctor.id, now()
    db.commit()
    return _one(db, t, current.staff)


@router.post("/treatments/{treatment_id}/visits", response_model=TreatmentOut, status_code=status.HTTP_201_CREATED)
def add_visit(treatment_id: uuid.UUID, payload: VisitIn, request: Request, current: Current = Depends(get_current)):
    """A sitting. On a planned treatment it starts the treatment. It can carry a
    prescription, finish the treatment, and book the next sitting."""
    db, clinic = current.db, current.clinic
    t = _treatment(db, treatment_id)
    if t.status in ("finished", "cancelled"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This treatment is {t.status}.")
    doctor = _doctor(db, payload.doctor_id)
    day = _day(clinic, payload.visit_date, not_before=t.started_on)
    today = clinic_today(clinic)
    if payload.next_visit is not None and payload.next_visit.due_on < max(today, day):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Choose a later date for the next sitting.")

    started_now = t.status == "planned"
    if started_now:
        _start(db, clinic, t, day, current.staff.id)
    visit = Visit(
        clinic_id=clinic.id, patient_id=t.patient_id, treatment_id=t.id, visit_date=day, doctor_id=doctor.id,
        notes=payload.notes, created_by=current.staff.id,
    )
    db.add(visit)
    db.flush()
    save_prescription(
        db, clinic, existing=None, data=payload.prescription, editor=current.staff, patient_id=t.patient_id,
        doctor_id=doctor.id, visit_id=visit.id,
    )
    if day == today:
        mark_seen(db, clinic, t.patient_id, visit_id=visit.id)
    if payload.finish_treatment:
        _finish(db, clinic, t, day, current.staff.id)
    if payload.next_visit is not None:
        service = db.get(Service, t.service_id)
        db.add(
            FollowUp(
                clinic_id=clinic.id, patient_id=t.patient_id, kind="visit", due_on=payload.next_visit.due_on,
                reason=payload.next_visit.reason or f"Next sitting: {service.name}", source="after_visit",
                treatment_id=t.id, created_by=current.staff.id,
            )
        )
    _log(db, current, request, "create", t, {
        "treatment_id": str(t.id), "visit_date": day.isoformat(), "doctor": doctor.name,
        "started_treatment": started_now, "finished_treatment": payload.finish_treatment,
        "prescription": payload.prescription is not None and not payload.prescription.is_empty(),
        "next_visit": _plain(payload.next_visit.due_on) if payload.next_visit else None,
    }, entity_type="visit", entity_id=visit.id)
    db.commit()
    return _one(db, t, current.staff)


@router.patch("/visits/{visit_id}", response_model=TreatmentOut)
def update_visit(visit_id: uuid.UUID, payload: VisitUpdate, request: Request, current: Current = Depends(get_current)):
    """Corrects a sitting. Only the prescribing doctor may change its prescription."""
    db, clinic = current.db, current.clinic
    visit = db.get(Visit, visit_id)
    if visit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this visit.")
    t = _treatment(db, visit.treatment_id)
    doctor = _doctor(db, payload.doctor_id) if payload.doctor_id != visit.doctor_id else db.get(Staff, visit.doctor_id)
    day = _day(clinic, payload.visit_date, not_before=t.started_on)
    if t.finished_on is not None and day > t.finished_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The date cannot be after the treatment finished on {date_text(t.finished_on)}.",
        )
    save_prescription(
        db, clinic, existing=db.scalar(select(Prescription).where(Prescription.visit_id == visit.id)),
        data=payload.prescription, editor=current.staff, patient_id=t.patient_id, doctor_id=doctor.id, visit_id=visit.id,
    )
    changes = {}
    for field, new in (("visit_date", day), ("doctor_id", doctor.id), ("notes", payload.notes)):
        old = getattr(visit, field)
        if old != new:
            changes[field] = [_plain(old), _plain(new)]
            setattr(visit, field, new)
    if changes:
        visit.updated_at = now()
        _log(db, current, request, "update", t, changes, entity_type="visit", entity_id=visit.id)
    db.commit()
    return _one(db, t, current.staff)
