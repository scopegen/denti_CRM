import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.deps import Current, require_owner
from app.models import AuditLog, Patient, Staff
from app.patients_util import patient_code
from app.schemas import AuditEntryView, AuditPage, AuditPatient, PersonBrief

# The Activity log: every change, view, download and sign in, with who, when, which patient
# and which device. Only the clinic owner can read it; nobody can edit or delete it (the API
# role may only add to it). The History on a record is this log filtered to that record.

router = APIRouter(prefix="/audit-log", tags=["audit log"])


@router.get("", response_model=AuditPage)
def activity_log(
    actor_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    entity_type: str | None = Query(default=None, max_length=40),
    entity_id: uuid.UUID | None = None,
    action: str | None = Query(default=None, max_length=40),
    date_from: date | None = None,
    date_to: date | None = None,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    current: Current = Depends(require_owner),
):
    db, clinic = current.db, current.clinic
    zone = ZoneInfo(clinic.timezone)
    stmt = select(AuditLog)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if patient_id is not None:
        stmt = stmt.where(AuditLog.patient_id == patient_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    # Dates are the clinic's own days.
    if date_from is not None:
        stmt = stmt.where(AuditLog.occurred_at >= datetime.combine(date_from, time.min, tzinfo=zone))
    if date_to is not None:
        stmt = stmt.where(AuditLog.occurred_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=zone))
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    rows = list(db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]

    staff = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).order_by(Staff.name))}
    patients = {
        p.id: AuditPatient(id=p.id, code=patient_code(clinic, p.patient_number), name=p.name)
        for p in db.scalars(select(Patient).where(Patient.id.in_({r.patient_id for r in rows if r.patient_id})))
    }
    return AuditPage(
        items=[
            AuditEntryView(
                id=r.id, action=r.action, actor_type=r.actor_type, actor=staff.get(r.actor_id) if r.actor_id else None,
                entity_type=r.entity_type, entity_id=r.entity_id, patient=patients.get(r.patient_id) if r.patient_id else None,
                changes=r.changes, ip=r.ip, device=r.device, occurred_at=r.occurred_at,
            )
            for r in rows
        ],
        next_before_id=rows[-1].id if more else None,
        people=list(staff.values()),
    )
