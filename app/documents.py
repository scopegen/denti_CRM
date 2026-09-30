"""Builds printable documents from the clinic's records. app.pdf only lays them out."""
from zoneinfo import ZoneInfo

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.files_service import read_file
from app.models import (
    Clinic,
    Consultation,
    ConsultationRecommendation,
    File,
    Patient,
    Prescription,
    PrescriptionVersion,
    Service,
    Staff,
    Treatment,
    Visit,
)
from app.patients_util import age_on, patient_code
from app.pdf import Doctor, Letterhead, PrescriptionDoc

_GENDER = {"male": "Male", "female": "Female", "other": "Other"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pdf_response(content: bytes, name: str, download: bool = False) -> Response:
    """A PDF to show in the browser, or to save when download is asked for. Never cached."""
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"},
    )


def date_text(day) -> str:
    """28 Sep 2026, the same way the app shows dates."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def letterhead_for(clinic: Clinic) -> Letterhead:
    # The clinic's address, phone, logo and design join this with the branding settings.
    return Letterhead(clinic_name=clinic.name)


def signature_png(db: Session, staff: Staff) -> bytes | None:
    if staff.signature_file_id is None:
        return None
    record = db.get(File, staff.signature_file_id)
    if record is None or record.deleted_at is not None:
        return None
    return read_file(record)


def doctor_for(db: Session, staff: Staff) -> Doctor:
    return Doctor(
        name=staff.name,
        qualification=staff.qualification,
        specialty=staff.specialty,
        registration_no=staff.registration_no,
        signature_png=signature_png(db, staff),
    )


def _age_gender(patient: Patient, day) -> str:
    age = age_on(patient.dob, patient.birth_year, day)
    parts = [f"{age} years" if age is not None else None, _GENDER.get(patient.gender or "")]
    return " / ".join(p for p in parts if p) or "Not recorded"


def prescription_document(
    db: Session, clinic: Clinic, prescription: Prescription, version: PrescriptionVersion
) -> PrescriptionDoc:
    consultation = db.get(Consultation, prescription.consultation_id) if prescription.consultation_id else None
    visit = db.get(Visit, prescription.visit_id) if prescription.visit_id else None
    patient = db.get(Patient, prescription.patient_id)
    doctor = db.get(Staff, prescription.prescribing_doctor_id)

    recommendations = []
    if consultation is not None:
        rows = db.scalars(
            select(ConsultationRecommendation).where(ConsultationRecommendation.consultation_id == consultation.id)
        ).all()
        names = {s.id: s.name for s in db.scalars(select(Service).where(Service.id.in_({r.service_id for r in rows})))}
        for r in rows:
            teeth = ", ".join(str(t) for t in r.teeth)
            name = names.get(r.service_id, "Treatment")
            recommendations.append(f"{name} (teeth {teeth})" if teeth else name)

    treatment_line = None
    if visit is not None:
        treatment = db.get(Treatment, visit.treatment_id)
        name = db.get(Service, treatment.service_id).name
        teeth = ", ".join(str(t) for t in treatment.teeth)
        treatment_line = f"{name}, teeth {teeth}" if teeth else name

    edited_day = version.edited_at.astimezone(ZoneInfo(clinic.timezone)).date()
    if consultation is not None:
        day = consultation.consult_date
    elif visit is not None:
        day = visit.visit_date
    else:
        day = edited_day
    return PrescriptionDoc(
        date_text=date_text(day),
        patient_name=patient.name,
        patient_code=patient_code(clinic, patient.patient_number),
        age_gender=_age_gender(patient, day),
        phone=patient.phone,
        doctor=doctor_for(db, doctor),
        diagnosis=version.diagnosis,
        items=list(version.items),
        advice=version.advice,
        chief_complaint=consultation.chief_complaint if consultation else None,
        oral_examination=consultation.oral_examination if consultation else None,
        xray_taken=bool(consultation and consultation.xray_taken),
        recommendations=recommendations,
        treatment=treatment_line,
        version_note=f"Version {version.version}. Updated {date_text(edited_day)}." if version.version > 1 else None,
    )
