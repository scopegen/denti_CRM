import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import effective_at
from app.clinic_settings import get_settings
from app.clinic_time import clinic_today
from app.deps import Current, get_current
from app.documents import letterhead_for, pdf_response, prescription_document
from app.models import (
    Clinic,
    Consultation,
    ConsultationRecommendation,
    LedgerEntry,
    Patient,
    Prescription,
    PrescriptionVersion,
    Service,
    Staff,
    ToothFinding,
)
from app.patients_util import patient_code
from app.pdf import prescription_pdf
from app.prescriptions import latest_versions, prescription_out, save_prescription
from app.routers.today import mark_seen
from app.schemas import (
    ConsultationIn,
    ConsultationOut,
    FindingBrief,
    FindingIn,
    PersonBrief,
    PrescriptionVersionOut,
    RecommendationIn,
    RecommendationOut,
    RxItem,
)
from app.security import now
from app.tooth_chart import MISSING, add_finding, clear_finding, require_chart_plan

# Consultations (the exam) and their prescriptions. A consultation and its prescription are
# saved together in one request, so one can never be saved without the other. Saving a
# consultation adds the clinic's consultation fee to the patient's bill and turns the
# patient's arrival today to Seen. Doctors never see the fee.

router = APIRouter(tags=["consultations"])

_CLINICAL_FIELDS = ["doctor_id", "consult_date", "chief_complaint", "oral_examination", "xray_taken", "recommendation_note"]


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


def _check_date(clinic: Clinic, value) -> None:
    if value is not None and value > clinic_today(clinic):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The date cannot be in the future.")


def _check_services(db: Session, recommendations: list[RecommendationIn]) -> None:
    ids = {r.service_id for r in recommendations}
    if not ids:
        return
    found = set(db.scalars(select(Service.id).where(Service.id.in_(ids))).all())
    if found != ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose treatments from this clinic's services.")


def _plain(value):
    """Audit log values as plain JSON: ids and dates as text."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _outs(db: Session, consultations: list[Consultation], viewer: Staff) -> list[ConsultationOut]:
    if not consultations:
        return []
    ids = [c.id for c in consultations]
    recs: dict[uuid.UUID, list[ConsultationRecommendation]] = {}
    for r in db.scalars(select(ConsultationRecommendation).where(ConsultationRecommendation.consultation_id.in_(ids))):
        recs.setdefault(r.consultation_id, []).append(r)
    service_names = {
        s.id: s.name
        for s in db.scalars(select(Service).where(Service.id.in_({r.service_id for rs in recs.values() for r in rs})))
    }
    prescriptions = {p.consultation_id: p for p in db.scalars(select(Prescription).where(Prescription.consultation_id.in_(ids)))}
    versions = latest_versions(db, [p.id for p in prescriptions.values()])
    doctors = {s.id: s for s in db.scalars(select(Staff).where(Staff.id.in_({c.doctor_id for c in consultations})))}
    findings: dict[uuid.UUID, list[FindingBrief]] = {}
    for f in db.scalars(
        select(ToothFinding).where(ToothFinding.consultation_id.in_(ids)).order_by(ToothFinding.tooth, ToothFinding.noted_at)
    ):
        findings.setdefault(f.consultation_id, []).append(
            FindingBrief(id=f.id, tooth=f.tooth, problem=f.problem, resolution=f.resolution)
        )

    out = []
    for c in consultations:
        p = prescriptions.get(c.id)
        out.append(
            ConsultationOut(
                id=c.id,
                patient_id=c.patient_id,
                doctor=PersonBrief(id=c.doctor_id, name=doctors[c.doctor_id].name),
                consult_date=c.consult_date,
                chief_complaint=c.chief_complaint,
                oral_examination=c.oral_examination,
                xray_taken=c.xray_taken,
                recommendations=[
                    RecommendationOut(service_id=r.service_id, service_name=service_names.get(r.service_id, "Service"), teeth=list(r.teeth))
                    for r in recs.get(c.id, [])
                ],
                recommendation_note=c.recommendation_note,
                prescription=prescription_out(db, p, versions[p.id], viewer) if p else None,
                findings=findings.get(c.id, []),
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
        )
    return out


def _sync_findings(db: Session, clinic: Clinic, consultation: Consultation, wanted: list[FindingIn], actor: uuid.UUID):
    """Makes the problems still open from this consultation match the form: new ones are
    marked, ones taken off are cleared (kept in the tooth's history). Returns the change for
    the audit log, or None."""
    if wanted:
        require_chart_plan(clinic)
    open_ones = {
        (f.tooth, f.problem): f
        for f in db.scalars(
            select(ToothFinding).where(ToothFinding.consultation_id == consultation.id, ToothFinding.resolution.is_(None))
        )
    }
    wanted_pairs = {(f.tooth, f.problem.strip()) for f in wanted}
    if wanted_pairs == set(open_ones):
        return None
    for pair, finding in open_ones.items():
        if pair not in wanted_pairs:
            clear_finding(finding, actor)
    # Problems first, then missing teeth, which clear any problem on the same tooth.
    for tooth, problem in sorted(wanted_pairs - set(open_ones), key=lambda p: (p[1] == MISSING, p[0])):
        add_finding(
            db, clinic, patient_id=consultation.patient_id, tooth=tooth, problem=problem, actor=actor,
            consultation_id=consultation.id,
        )
    return [sorted(open_ones), sorted(wanted_pairs)]


def _set_recommendations(db: Session, clinic: Clinic, consultation: Consultation, items: list[RecommendationIn]) -> None:
    db.execute(delete(ConsultationRecommendation).where(ConsultationRecommendation.consultation_id == consultation.id))
    for item in items:
        db.add(
            ConsultationRecommendation(
                clinic_id=clinic.id, consultation_id=consultation.id, service_id=item.service_id, teeth=item.teeth
            )
        )


@router.get("/patients/{patient_id}/consultations", response_model=list[ConsultationOut])
def list_consultations(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    db = current.db
    _patient(db, patient_id)
    rows = db.scalars(
        select(Consultation)
        .where(Consultation.patient_id == patient_id)
        .order_by(Consultation.consult_date.desc(), Consultation.created_at.desc())
    ).all()
    return _outs(db, list(rows), current.staff)


@router.post("/patients/{patient_id}/consultations", response_model=ConsultationOut, status_code=status.HTTP_201_CREATED)
def create_consultation(patient_id: uuid.UUID, payload: ConsultationIn, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    doctor = _doctor(db, payload.doctor_id)
    _check_date(clinic, payload.consult_date)
    _check_services(db, payload.recommendations)
    day = payload.consult_date or clinic_today(clinic)

    consultation = Consultation(
        clinic_id=clinic.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        consult_date=day,
        chief_complaint=payload.chief_complaint,
        oral_examination=payload.oral_examination,
        xray_taken=payload.xray_taken,
        recommendation_note=payload.recommendation_note,
        created_by=current.staff.id,
    )
    db.add(consultation)
    db.flush()
    _set_recommendations(db, clinic, consultation, payload.recommendations)
    _sync_findings(db, clinic, consultation, payload.findings, current.staff.id)
    save_prescription(
        db, clinic, existing=None, data=payload.prescription, editor=current.staff, patient_id=patient.id,
        doctor_id=doctor.id, consultation_id=consultation.id,
    )

    fee = get_settings(db, clinic).default_consultation_fee_paise
    if fee > 0:
        db.add(
            LedgerEntry(
                clinic_id=clinic.id, patient_id=patient.id, kind="consultation_fee", amount_paise=fee,
                consultation_id=consultation.id, effective_at=effective_at(clinic, day), created_by=current.staff.id,
            )
        )

    if day == clinic_today(clinic):
        mark_seen(db, clinic, patient.id, consultation_id=consultation.id)

    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="consultation",
        entity_id=consultation.id, patient_id=patient.id,
        changes={"doctor": doctor.name, "date": day.isoformat(), "prescription": payload.prescription is not None},
        request=request,
    )
    db.commit()
    return _outs(db, [consultation], current.staff)[0]


@router.patch("/consultations/{consultation_id}", response_model=ConsultationOut)
def update_consultation(consultation_id: uuid.UUID, payload: ConsultationIn, request: Request, current: Current = Depends(get_current)):
    """Edits the clinical record. Only the prescribing doctor may change the prescription; a
    new version is saved only when its words actually change."""
    db, clinic = current.db, current.clinic
    consultation = db.get(Consultation, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this consultation.")
    doctor = _doctor(db, payload.doctor_id) if payload.doctor_id != consultation.doctor_id else db.get(Staff, consultation.doctor_id)
    _check_date(clinic, payload.consult_date)
    _check_services(db, payload.recommendations)

    save_prescription(
        db, clinic,
        existing=db.scalar(select(Prescription).where(Prescription.consultation_id == consultation.id)),
        data=payload.prescription, editor=current.staff, patient_id=consultation.patient_id,
        doctor_id=doctor.id, consultation_id=consultation.id,
    )

    new_values = {
        "doctor_id": doctor.id,
        "consult_date": payload.consult_date or consultation.consult_date,
        "chief_complaint": payload.chief_complaint,
        "oral_examination": payload.oral_examination,
        "xray_taken": payload.xray_taken,
        "recommendation_note": payload.recommendation_note,
    }
    changes = {}
    for field in _CLINICAL_FIELDS:
        old, new = getattr(consultation, field), new_values[field]
        if old != new:
            changes[field] = [_plain(old), _plain(new)]
            setattr(consultation, field, new)

    old_recs = sorted(
        (str(r.service_id), list(r.teeth))
        for r in db.scalars(select(ConsultationRecommendation).where(ConsultationRecommendation.consultation_id == consultation.id))
    )
    new_recs = sorted((str(r.service_id), r.teeth) for r in payload.recommendations)
    if old_recs != new_recs:
        changes["recommendations"] = [old_recs, new_recs]
        _set_recommendations(db, clinic, consultation, payload.recommendations)

    findings_change = _sync_findings(db, clinic, consultation, payload.findings, current.staff.id)
    if findings_change:
        changes["findings"] = findings_change

    if changes:
        consultation.updated_at = now()
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="consultation",
            entity_id=consultation.id, patient_id=consultation.patient_id, changes=changes, request=request,
        )
    db.commit()
    return _outs(db, [consultation], current.staff)[0]


@router.get("/prescriptions/{prescription_id}/versions", response_model=list[PrescriptionVersionOut])
def prescription_versions(prescription_id: uuid.UUID, current: Current = Depends(get_current)):
    """Every version ever saved, newest first. Nothing is overwritten."""
    db = current.db
    if db.get(Prescription, prescription_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this prescription.")
    versions = db.scalars(
        select(PrescriptionVersion).where(PrescriptionVersion.prescription_id == prescription_id).order_by(PrescriptionVersion.version.desc())
    ).all()
    editors = {s.id: s.name for s in db.scalars(select(Staff).where(Staff.id.in_({v.edited_by for v in versions})))}
    return [
        PrescriptionVersionOut(
            version=v.version, diagnosis=v.diagnosis, advice=v.advice, items=[RxItem(**i) for i in v.items],
            edited_by=PersonBrief(id=v.edited_by, name=editors.get(v.edited_by, "Unknown")), edited_at=v.edited_at,
        )
        for v in versions
    ]


@router.get("/prescriptions/{prescription_id}/pdf")
def prescription_pdf_file(
    prescription_id: uuid.UUID,
    request: Request,
    download: bool = False,
    version: int | None = Query(default=None, ge=1),
    current: Current = Depends(get_current),
):
    """The printable prescription, with the prescribing doctor's signature. The latest
    version unless an earlier one is asked for."""
    db, clinic = current.db, current.clinic
    prescription = db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this prescription.")
    wanted = version or prescription.current_version
    content, name = build_prescription_pdf(db, clinic, prescription, wanted)
    record(
        db, clinic_id=clinic.id, action="download" if download else "view", actor_id=current.staff.id,
        entity_type="prescription", entity_id=prescription.id, patient_id=prescription.patient_id,
        changes={"document": "prescription pdf", "version": wanted}, request=request,
    )
    db.commit()
    return pdf_response(content, name, download)


def build_prescription_pdf(db: Session, clinic: Clinic, prescription: Prescription, version: int) -> tuple[bytes, str]:
    """The PDF and its file name. Also used for a link sent to the patient."""
    chosen = db.scalar(
        select(PrescriptionVersion).where(
            PrescriptionVersion.prescription_id == prescription.id, PrescriptionVersion.version == version
        )
    )
    if chosen is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this version.")
    doc = prescription_document(db, clinic, prescription, chosen)
    patient = db.get(Patient, prescription.patient_id)
    name = f"Prescription {patient_code(clinic, patient.patient_number)} {doc.date_text}.pdf"
    return prescription_pdf(letterhead_for(clinic), doc), name
