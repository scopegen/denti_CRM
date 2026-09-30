import uuid
from datetime import date, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import format_inr
from app.clinic_settings import get_settings
from app.deps import Current, get_current, require_admin
from app.documents import date_text
from app.ids import uuid7
from app.models import (
    Clinic,
    Estimate,
    FollowUp,
    Invoice,
    MessageLog,
    MessageTemplate,
    Patient,
    Prescription,
    Service,
    ShareLink,
    Staff,
    Treatment,
)
from app.routers.estimates import number_text as estimate_number
from app.routers.invoices import number_text as invoice_number
from app.schemas import (
    MessageIn,
    MessageOpened,
    MessageOut,
    MessageTemplateIn,
    MessageTemplateOut,
    PersonBrief,
    ShareLinkOut,
    WhatsAppSettingsIn,
    WhatsAppSettingsOut,
)
from app.security import now
from app.whatsapp import (
    DEFAULTS,
    KINDS,
    LANGUAGES,
    PLACEHOLDERS,
    chat_url,
    check_wording,
    first_name,
    new_share_link,
    render,
    saved_wording,
    whatsapp_number,
    wording,
)

# WhatsApp tap-to-send (Standard and Pro). Tapping a WhatsApp button asks here for the message:
# the server writes it from the clinic's wording, adds a secure link when it carries a
# document, logs it, and answers with a wa.me address that opens WhatsApp ready to send.

router = APIRouter(tags=["whatsapp"])

# Doctors never see amounts, so invoices and estimates are sent by the owner or receptionist.
_ADMIN_ONLY = {"invoice", "estimate"}


def require_whatsapp_plan(clinic: Clinic) -> None:
    if clinic.plan == "basic":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="WhatsApp messages come with the Standard and Pro plans.")


def _templates(db: Session, clinic: Clinic) -> list[MessageTemplateOut]:
    saved = saved_wording(db, clinic)
    return [
        MessageTemplateOut(
            kind=kind, language=language, body=saved[(kind, language)].body if (kind, language) in saved else DEFAULTS[(kind, language)],
            default_body=DEFAULTS[(kind, language)], is_default=(kind, language) not in saved, placeholders=list(PLACEHOLDERS[kind]),
        )
        for kind in KINDS
        for language in LANGUAGES
    ]


def _settings_out(db: Session, clinic: Clinic) -> WhatsAppSettingsOut:
    s = get_settings(db, clinic)
    return WhatsAppSettingsOut(language=s.whatsapp_language, google_review_url=s.google_review_url, templates=_templates(db, clinic))


@router.get("/whatsapp/settings", response_model=WhatsAppSettingsOut)
def whatsapp_settings(current: Current = Depends(require_admin)):
    require_whatsapp_plan(current.clinic)
    out = _settings_out(current.db, current.clinic)
    current.db.commit()
    return out


@router.put("/whatsapp/settings", response_model=WhatsAppSettingsOut)
def update_whatsapp_settings(payload: WhatsAppSettingsIn, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    require_whatsapp_plan(clinic)
    s = get_settings(db, clinic)
    changes = {}
    if s.whatsapp_language != payload.language:
        changes["whatsapp_language"] = [s.whatsapp_language, payload.language]
        s.whatsapp_language = payload.language
    if s.google_review_url != payload.google_review_url:
        changes["google_review_url"] = [s.google_review_url, payload.google_review_url]
        s.google_review_url = payload.google_review_url
    if changes:
        s.updated_at = now()
        record(db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="clinic_settings", changes=changes, request=request)
    db.commit()
    return _settings_out(db, clinic)


def _kind_and_language(kind: str, language: str) -> None:
    if kind not in KINDS or language not in LANGUAGES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="There is no such message.")


@router.put("/whatsapp/templates/{kind}/{language}", response_model=WhatsAppSettingsOut)
def save_template(kind: str, language: str, payload: MessageTemplateIn, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    require_whatsapp_plan(clinic)
    _kind_and_language(kind, language)
    body = check_wording(kind, payload.body)
    before = wording(db, clinic, kind, language)
    saved = db.get(MessageTemplate, {"clinic_id": clinic.id, "kind": kind, "language": language})
    if body == DEFAULTS[(kind, language)]:
        # The default wording typed back in: keep following Denti's default.
        if saved is not None:
            db.delete(saved)
    elif saved is None:
        db.add(MessageTemplate(clinic_id=clinic.id, kind=kind, language=language, body=body, updated_by=current.staff.id))
    else:
        saved.body, saved.updated_by, saved.updated_at = body, current.staff.id, now()
    if body != before:
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="message_template",
            changes={"message": f"{kind} ({language})", "wording": [before, body]}, request=request,
        )
    db.commit()
    return _settings_out(db, clinic)


@router.delete("/whatsapp/templates/{kind}/{language}", response_model=WhatsAppSettingsOut)
def reset_template(kind: str, language: str, request: Request, current: Current = Depends(require_admin)):
    """Back to Denti's default wording."""
    db, clinic = current.db, current.clinic
    require_whatsapp_plan(clinic)
    _kind_and_language(kind, language)
    before = wording(db, clinic, kind, language)
    removed = db.execute(
        delete(MessageTemplate).where(MessageTemplate.kind == kind, MessageTemplate.language == language)
    ).rowcount
    if removed:
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="message_template",
            changes={"message": f"{kind} ({language})", "wording": [before, DEFAULTS[(kind, language)]]}, request=request,
        )
    db.commit()
    return _settings_out(db, clinic)


def _when(day: date, at: time | None) -> str:
    if at is None:
        return date_text(day)
    hour = at.hour % 12 or 12
    return f"{date_text(day)} at {hour}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def _belongs(row, patient: Patient, what: str):
    if row is None or row.patient_id != patient.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"We could not find this {what} for the patient.")
    return row


def _needs(value, what: str):
    if value is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Say which {what} the message is about.")
    return value


@router.post("/whatsapp/messages", response_model=MessageOpened)
def open_message(payload: MessageIn, request: Request, current: Current = Depends(get_current)):
    """Writes the message and logs it. WhatsApp itself opens on the person's device, where
    they press send."""
    db, clinic, staff = current.db, current.clinic, current.staff
    require_whatsapp_plan(clinic)
    kind = payload.kind
    if kind in _ADMIN_ONLY and staff.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner or receptionist can send invoices and estimates.")
    if kind == "lab_ready":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Lab messages are sent from Lab work.")

    patient = db.get(Patient, payload.patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    if patient.whatsapp_consent_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{patient.name} has not agreed to WhatsApp messages. Tick WhatsApp consent on their details first.",
        )
    number = whatsapp_number(patient.phone)
    if number is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{patient.phone} cannot be used on WhatsApp. Check the phone number."
        )

    settings_row = get_settings(db, clinic)
    language = payload.language or settings_row.whatsapp_language
    values = {"name": first_name(patient.name), "clinic": clinic.name}
    log = MessageLog(id=uuid7(), clinic_id=clinic.id, patient_id=patient.id, kind=kind, language=language, sent_by=staff.id)
    link = None

    if kind in ("visit_reminder", "call_reminder"):
        follow_up = _belongs(db.get(FollowUp, _needs(payload.follow_up_id, "follow up")), patient, "follow up")
        if (kind == "visit_reminder") != (follow_up.kind == "visit"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="This message does not fit that follow up.")
        if follow_up.status != "upcoming":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This follow up is already done or cancelled.")
        values |= {"date": _when(follow_up.due_on, follow_up.due_time), "reason": follow_up.reason or ""}
        log.follow_up_id = follow_up.id
    elif kind == "prescription":
        prescription = _belongs(db.get(Prescription, _needs(payload.prescription_id, "prescription")), patient, "prescription")
        doctor = db.get(Staff, prescription.prescribing_doctor_id)
        link, url = new_share_link(
            db, clinic, patient_id=patient.id, kind="prescription", created_by=staff.id,
            prescription_id=prescription.id, prescription_version=prescription.current_version,
        )
        values |= {"doctor": doctor.name, "date": date_text(prescription.created_at.astimezone(ZoneInfo(clinic.timezone)).date()), "link": url}
    elif kind == "invoice":
        invoice = _belongs(db.get(Invoice, _needs(payload.invoice_id, "invoice")), patient, "invoice")
        if invoice.status == "cancelled":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is cancelled.")
        link, url = new_share_link(db, clinic, patient_id=patient.id, kind="invoice", created_by=staff.id, invoice_id=invoice.id)
        values |= {"number": invoice_number(clinic, invoice), "amount": format_inr(invoice.total_paise), "link": url}
    elif kind == "estimate":
        estimate = _belongs(db.get(Estimate, _needs(payload.estimate_id, "estimate")), patient, "estimate")
        link, url = new_share_link(db, clinic, patient_id=patient.id, kind="estimate", created_by=staff.id, estimate_id=estimate.id)
        values |= {"number": estimate_number(estimate), "valid_until": date_text(estimate.valid_until), "link": url}
    elif kind == "review_request":
        treatment = _belongs(db.get(Treatment, _needs(payload.treatment_id, "treatment")), patient, "treatment")
        if treatment.status != "finished":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask for a review once the treatment is finished.")
        if not settings_row.google_review_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Add your clinic's Google review link in Settings, WhatsApp first."
            )
        values |= {"treatment": db.get(Service, treatment.service_id).name.lower(), "review_link": settings_row.google_review_url}
        log.treatment_id = treatment.id

    body = render(wording(db, clinic, kind, language), values)
    log.body, log.to_phone = body, number
    log.share_link_id = link.id if link else None
    db.add(log)
    record(
        db, clinic_id=clinic.id, action="send", actor_id=staff.id, entity_type="message", entity_id=log.id,
        patient_id=patient.id, changes={"message": kind, "language": language, "secure_link": link is not None}, request=request,
    )
    db.commit()
    return MessageOpened(url=chat_url(number, body), phone=number, body=body, link_expires_at=link.expires_at if link else None)


def _link_out(link: ShareLink) -> ShareLinkOut:
    return ShareLinkOut(
        id=link.id, expires_at=link.expires_at, revoked_at=link.revoked_at, open_count=link.open_count,
        last_opened_at=link.last_opened_at, active=link.revoked_at is None and link.expires_at > now(),
    )


@router.get("/patients/{patient_id}/messages", response_model=list[MessageOut])
def patient_messages(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    """Every WhatsApp message opened for this patient, newest first, with its link if any."""
    db = current.db
    require_whatsapp_plan(current.clinic)
    rows = db.scalars(select(MessageLog).where(MessageLog.patient_id == patient_id).order_by(MessageLog.created_at.desc())).all()
    links = {l.id: l for l in db.scalars(select(ShareLink).where(ShareLink.id.in_({r.share_link_id for r in rows if r.share_link_id})))}
    people = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_({r.sent_by for r in rows if r.sent_by})))}
    # Doctors never see amounts, and invoice and estimate messages carry them.
    hide = current.staff.role == "doctor"
    return [
        MessageOut(
            id=r.id, kind=r.kind, language=r.language,
            body="Invoice or estimate sent on WhatsApp." if hide and r.kind in _ADMIN_ONLY else r.body,
            to_phone=r.to_phone, sent_by=people.get(r.sent_by), created_at=r.created_at,
            link=_link_out(links[r.share_link_id]) if r.share_link_id and not (hide and r.kind in _ADMIN_ONLY) else None,
        )
        for r in rows
    ]


@router.post("/share-links/{link_id}/stop", response_model=ShareLinkOut)
def stop_link(link_id: uuid.UUID, request: Request, current: Current = Depends(require_admin)):
    """Stops a shared link at once, for example when a message went to the wrong number."""
    db, clinic = current.db, current.clinic
    link = db.get(ShareLink, link_id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this link.")
    if link.revoked_at is None:
        link.revoked_at, link.revoked_by = now(), current.staff.id
        record(
            db, clinic_id=clinic.id, action="cancel", actor_id=current.staff.id, entity_type="share_link", entity_id=link.id,
            patient_id=link.patient_id, changes={"document": link.kind}, request=request,
        )
    db.commit()
    return _link_out(link)
