from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.db import get_db, use_clinic
from app.documents import date_text, pdf_response
from app.models import Clinic, Estimate, Invoice, Prescription, ShareLink
from app.routers.consultations import build_prescription_pdf
from app.routers.estimates import build_estimate_pdf
from app.routers.estimates import number_text as estimate_number
from app.routers.invoices import build_invoice_pdf
from app.routers.invoices import number_text as invoice_number
from app.schemas import SharedDocumentOut
from app.security import now
from app.whatsapp import find_share_link

# A document sent to a patient on WhatsApp, opened from the link in the message without
# signing in. The link names the clinic (denti.in/<clinic>/d/<token>); the token is long and
# random, only its hash is stored, and it stops working after 30 days or when the clinic
# stops it. Every opening is written to the clinic's Activity log.

router = APIRouter(prefix="/public/{clinic_slug}/documents", tags=["shared documents"])


def _open(db: Session, clinic_slug: str, token: str) -> tuple[Clinic, ShareLink]:
    clinic = db.scalar(select(Clinic).where(Clinic.slug == clinic_slug.strip().lower()))
    if clinic is None or clinic.status == "closed":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is not valid.")
    use_clinic(db, clinic.id)
    link = find_share_link(db, token)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is not valid.")
    if link.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="The clinic has stopped this link. Please ask them to send it again.")
    if link.expires_at <= now():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This link has expired. Please ask the clinic to send it again.")
    return clinic, link


def _describe(db: Session, clinic: Clinic, link: ShareLink) -> tuple[str, str]:
    """The document's title and date, as the patient sees them."""
    if link.kind == "prescription":
        prescription = db.get(Prescription, link.prescription_id)
        return "Prescription", date_text(prescription.created_at.astimezone(ZoneInfo(clinic.timezone)).date())
    if link.kind == "invoice":
        invoice = db.get(Invoice, link.invoice_id)
        return f"Invoice {invoice_number(clinic, invoice)}", date_text(invoice.issued_on)
    estimate = db.get(Estimate, link.estimate_id)
    return f"Treatment estimate {estimate_number(estimate)}", date_text(estimate.given_on)


@router.get("/{token}", response_model=SharedDocumentOut)
def shared_document(clinic_slug: str, token: str, db: Session = Depends(get_db)):
    clinic, link = _open(db, clinic_slug, token)
    title, day = _describe(db, clinic, link)
    return SharedDocumentOut(clinic_name=clinic.name, kind=link.kind, title=title, date_text=day, expires_at=link.expires_at)


@router.get("/{token}/pdf")
def shared_document_pdf(clinic_slug: str, token: str, request: Request, download: bool = False, db: Session = Depends(get_db)):
    clinic, link = _open(db, clinic_slug, token)
    if link.kind == "prescription":
        # The version that was sent, even if the prescription was edited since.
        content, name = build_prescription_pdf(db, clinic, db.get(Prescription, link.prescription_id), link.prescription_version)
        entity_id = link.prescription_id
    elif link.kind == "invoice":
        content, name = build_invoice_pdf(db, clinic, db.get(Invoice, link.invoice_id))
        entity_id = link.invoice_id
    else:
        content, name = build_estimate_pdf(db, clinic, db.get(Estimate, link.estimate_id))
        entity_id = link.estimate_id
    link.open_count += 1
    link.last_opened_at = now()
    record(
        db, clinic_id=clinic.id, action="download" if download else "view", actor_type="patient", entity_type=link.kind,
        entity_id=entity_id, patient_id=link.patient_id, changes={"document": f"{link.kind} pdf", "via": "shared link"},
        request=request,
    )
    db.commit()
    return pdf_response(content, name, download)
