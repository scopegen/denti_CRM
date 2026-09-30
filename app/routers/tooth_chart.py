import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.clinic_time import clinic_today
from app.deps import Current, get_current
from app.models import Clinic, Consultation, MouthCheck, Patient, Service, Staff, ToothFinding, Treatment
from app.schemas import (
    ChartTreatment,
    FindingCreate,
    FindingOut,
    MouthCheckOut,
    PersonBrief,
    ServiceBrief,
    ToothChartOut,
)
from app.tooth_chart import add_finding, clear_finding, require_chart_plan, statuses, tooth_problems

# The patient's tooth chart (Standard and Pro): its colours, the problems found on teeth and
# their history, and the full mouth checks. Everyone who treats or registers patients uses
# it. Every change answers with the whole chart, so the screen always shows what the server
# worked out.

router = APIRouter(tags=["tooth chart"])


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def chart_for(db: Session, clinic: Clinic, patient_id: uuid.UUID) -> ToothChartOut:
    findings = list(
        db.scalars(select(ToothFinding).where(ToothFinding.patient_id == patient_id).order_by(ToothFinding.noted_at.desc()))
    )
    treatments = list(
        db.scalars(
            select(Treatment)
            .where(Treatment.patient_id == patient_id, Treatment.status != "cancelled")
            .order_by(Treatment.created_at.desc())
        )
    )
    check = db.scalar(
        select(MouthCheck).where(MouthCheck.patient_id == patient_id).order_by(MouthCheck.checked_on.desc(), MouthCheck.created_at.desc())
    )
    staff_ids = {f.noted_by for f in findings} | {f.resolved_by for f in findings if f.resolved_by} | {t.doctor_id for t in treatments}
    if check:
        staff_ids.add(check.checked_by)
    people = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_(staff_ids)))}
    names = {s.id: s.name for s in db.scalars(select(Service).where(Service.id.in_({t.service_id for t in treatments})))}

    return ToothChartOut(
        numbering=clinic.tooth_numbering,
        statuses=statuses(findings, treatments),
        findings=[
            FindingOut(
                id=f.id, tooth=f.tooth, problem=f.problem, consultation_id=f.consultation_id, noted_by=people[f.noted_by],
                noted_at=f.noted_at, resolution=f.resolution, resolved_at=f.resolved_at,
                resolved_by=people.get(f.resolved_by) if f.resolved_by else None,
                resolved_by_treatment_id=f.resolved_by_treatment_id,
            )
            for f in findings
        ],
        treatments=[
            ChartTreatment(
                id=t.id, service=ServiceBrief(id=t.service_id, name=names[t.service_id]), status=t.status,
                teeth=list(t.teeth), doctor=people[t.doctor_id], started_on=t.started_on, finished_on=t.finished_on,
                created_at=t.created_at,
            )
            for t in treatments
            if t.teeth
        ],
        last_mouth_check=MouthCheckOut(checked_on=check.checked_on, checked_by=people[check.checked_by]) if check else None,
        problems=tooth_problems(db),
    )


@router.get("/patients/{patient_id}/tooth-chart", response_model=ToothChartOut)
def get_chart(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    require_chart_plan(current.clinic)
    _patient(current.db, patient_id)
    return chart_for(current.db, current.clinic, patient_id)


@router.post("/patients/{patient_id}/findings", response_model=ToothChartOut, status_code=status.HTTP_201_CREATED)
def mark_problem(patient_id: uuid.UUID, payload: FindingCreate, request: Request, current: Current = Depends(get_current)):
    """Marks a problem on a tooth, or the tooth missing."""
    db, clinic = current.db, current.clinic
    require_chart_plan(clinic)
    patient = _patient(db, patient_id)
    if payload.consultation_id is not None:
        consultation = db.get(Consultation, payload.consultation_id)
        if consultation is None or consultation.patient_id != patient.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That consultation is not this patient's.")
    finding = add_finding(
        db, clinic, patient_id=patient.id, tooth=payload.tooth, problem=payload.problem, actor=current.staff.id,
        consultation_id=payload.consultation_id,
    )
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="tooth_finding",
        entity_id=finding.id, patient_id=patient.id, changes={"tooth": finding.tooth, "problem": finding.problem},
        request=request,
    )
    db.commit()
    return chart_for(db, clinic, patient.id)


@router.post("/findings/{finding_id}/clear", response_model=ToothChartOut)
def clear_problem(finding_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    """No longer a problem, entered by mistake, or a missing tooth marked present again. The
    finding stays in the tooth's history."""
    db, clinic = current.db, current.clinic
    require_chart_plan(clinic)
    finding = db.get(ToothFinding, finding_id)
    if finding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this problem.")
    if finding.resolution is None:
        clear_finding(finding, current.staff.id)
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="tooth_finding",
            entity_id=finding.id, patient_id=finding.patient_id,
            changes={"tooth": finding.tooth, "problem": finding.problem, "resolution": [None, "cleared"]}, request=request,
        )
        db.commit()
    return chart_for(db, clinic, finding.patient_id)


@router.post("/patients/{patient_id}/mouth-checks", response_model=ToothChartOut, status_code=status.HTTP_201_CREATED)
def full_mouth_check(patient_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    """Every tooth was looked at today; teeth with nothing marked are healthy."""
    db, clinic = current.db, current.clinic
    require_chart_plan(clinic)
    patient = _patient(db, patient_id)
    check = MouthCheck(clinic_id=clinic.id, patient_id=patient.id, checked_on=clinic_today(clinic), checked_by=current.staff.id)
    db.add(check)
    db.flush()
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="mouth_check",
        entity_id=check.id, patient_id=patient.id, request=request,
    )
    db.commit()
    return chart_for(db, clinic, patient.id)
