"""Builds printable documents from the clinic's records. app.pdf only lays them out."""
from zoneinfo import ZoneInfo

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic_settings import get_settings
from app.files_service import read_file
from app.models import (
    Clinic,
    ClinicSettings,
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


BRANDED_PLANS = ("standard", "pro")


def logo_png(db: Session, settings: ClinicSettings) -> bytes | None:
    if settings.logo_file_id is None:
        return None
    stored = db.get(File, settings.logo_file_id)
    if stored is None or stored.deleted_at is not None:
        return None
    return read_file(stored)


def letterhead_for(db: Session, clinic: Clinic) -> Letterhead:
    """The letterhead from the clinic's details: its logo, tagline, address, contact line and
    GSTIN. Its colour and design count on Standard and Pro only, and hiding "Powered by Denti" on
    Pro only, so a clinic that moves to a smaller plan simply prints the plain letterhead."""
    settings = get_settings(db, clinic)
    # The address prints on one line (the lines it was typed in joined with commas), so the
    # letterhead stays short enough for every design.
    address = ", ".join(line.strip() for line in (settings.address or "").splitlines() if line.strip())
    lines = [address] if address else []
    contact = " · ".join(x for x in [settings.phone, settings.email, settings.website] if x)
    if contact:
        lines.append(contact)
    if settings.gstin:
        lines.append(f"GSTIN {settings.gstin}")
    branded = clinic.plan in BRANDED_PLANS
    return Letterhead(
        clinic_name=clinic.name,
        tagline=settings.tagline,
        lines=lines,
        logo_png=logo_png(db, settings),
        accent=settings.brand_colour if branded else None,
        design=settings.letterhead_design if branded else "classic",
        footer_text=settings.footer_text,
        show_powered_by=not (clinic.plan == "pro" and settings.hide_powered_by),
        preprinted=settings.preprinted_letterhead,
        top_margin_mm=settings.preprinted_top_margin_mm,
    )


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
