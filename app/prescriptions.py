"""Prescriptions, written at a consultation or at a treatment visit. The words live in
PrescriptionVersion rows: a real change adds a version, nothing is overwritten, and only the
prescribing doctor may make one."""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Clinic, Prescription, PrescriptionVersion, Staff
from app.schemas import PersonBrief, PrescriptionIn, PrescriptionOut, RxItem


def add_version(db: Session, clinic: Clinic, prescription: Prescription, data: PrescriptionIn, editor: Staff) -> None:
    db.add(
        PrescriptionVersion(
            clinic_id=clinic.id,
            prescription_id=prescription.id,
            version=prescription.current_version,
            diagnosis=data.diagnosis,
            advice=data.advice,
            items=[item.model_dump() for item in data.items],
            edited_by=editor.id,
        )
    )


def same_content(version: PrescriptionVersion, data: PrescriptionIn) -> bool:
    return (
        version.diagnosis == data.diagnosis
        and version.advice == data.advice
        and version.items == [item.model_dump() for item in data.items]
    )


def latest_versions(db: Session, prescription_ids: list[uuid.UUID]) -> dict[uuid.UUID, PrescriptionVersion]:
    if not prescription_ids:
        return {}
    latest: dict[uuid.UUID, PrescriptionVersion] = {}
    for v in db.scalars(
        select(PrescriptionVersion)
        .where(PrescriptionVersion.prescription_id.in_(prescription_ids))
        .order_by(PrescriptionVersion.version)
    ):
        latest[v.prescription_id] = v
    return latest


def prescription_out(db: Session, prescription: Prescription, version: PrescriptionVersion, viewer: Staff) -> PrescriptionOut:
    doctor = db.get(Staff, prescription.prescribing_doctor_id)
    return PrescriptionOut(
        id=prescription.id,
        prescribing_doctor=PersonBrief(id=doctor.id, name=doctor.name),
        version=version.version,
        diagnosis=version.diagnosis,
        advice=version.advice,
        items=[RxItem(**item) for item in version.items],
        edited_at=version.edited_at,
        can_edit=viewer.id == prescription.prescribing_doctor_id,
    )


def save_prescription(
    db: Session,
    clinic: Clinic,
    *,
    existing: Prescription | None,
    data: PrescriptionIn | None,
    editor: Staff,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    consultation_id: uuid.UUID | None = None,
    visit_id: uuid.UUID | None = None,
) -> None:
    """Writes a new prescription, or a new version of the existing one when its words change.
    An empty prescription writes nothing; an existing one is never removed."""
    wanted = data if data is not None and not data.is_empty() else None
    if existing is not None:
        latest = latest_versions(db, [existing.id])[existing.id]
        if wanted is not None and not same_content(latest, wanted):
            if editor.id != existing.prescribing_doctor_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only the doctor who wrote this prescription can change it.",
                )
            existing.current_version += 1
            add_version(db, clinic, existing, wanted, editor)
    elif wanted is not None:
        prescription = Prescription(
            clinic_id=clinic.id, patient_id=patient_id, consultation_id=consultation_id, visit_id=visit_id,
            prescribing_doctor_id=doctor_id, created_by=editor.id,
        )
        db.add(prescription)
        db.flush()
        add_version(db, clinic, prescription, wanted, editor)
