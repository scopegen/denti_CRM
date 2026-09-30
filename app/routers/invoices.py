import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import format_inr
from app.clinic_time import clinic_today
from app.counters import take_next
from app.deps import Current, require_admin
from app.documents import date_text, letterhead_for, pdf_response
from app.models import Clinic, Consultation, Invoice, InvoiceLine, LedgerEntry, Patient, Service, Staff, Treatment
from app.patients_util import patient_code, patient_prefix
from app.pdf import InvoiceDoc, InvoiceLineDoc, invoice_pdf
from app.schemas import (
    InvoiceableItem,
    InvoiceCancel,
    InvoiceCreate,
    InvoiceLineOut,
    InvoiceOut,
    InvoicesOut,
    PersonBrief,
)
from app.security import now

# Invoices, for the owner and receptionist: a printable document for consultation fees and
# treatments already on the bill. Numbered per financial year (1 April to 31 March), such as
# SMILE/26-27/0001. An item goes on one invoice at a time; cancelling an invoice keeps its
# number and frees its items for a new one. The bill itself never changes because of it.

router = APIRouter(tags=["invoices"])


def financial_year(day: date) -> str:
    """2026-27 for any day from 1 April 2026 to 31 March 2027."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def number_text(clinic: Clinic, invoice: Invoice) -> str:
    return f"{patient_prefix(clinic)}/{invoice.financial_year[2:]}/{invoice.number:04d}"


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def _on_issued_invoices(db: Session, patient_id: uuid.UUID) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    rows = db.execute(
        select(InvoiceLine.consultation_id, InvoiceLine.treatment_id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.patient_id == patient_id, Invoice.status == "issued")
    ).all()
    return {c for c, _ in rows if c}, {t for _, t in rows if t}


def invoiceable(db: Session, patient_id: uuid.UUID) -> list[InvoiceableItem]:
    """Consultation fees and started treatments on the bill that are on no issued invoice."""
    taken_consultations, taken_treatments = _on_issued_invoices(db, patient_id)
    items: list[InvoiceableItem] = []

    fees = db.execute(
        select(Consultation, func.sum(LedgerEntry.amount_paise), Staff.name)
        .join(LedgerEntry, LedgerEntry.consultation_id == Consultation.id)
        .join(Staff, Staff.id == Consultation.doctor_id)
        .where(Consultation.patient_id == patient_id, LedgerEntry.kind == "consultation_fee")
        .group_by(Consultation.id, Staff.name)
    ).all()
    for c, amount, doctor in fees:
        if c.id not in taken_consultations and amount:
            items.append(
                InvoiceableItem(kind="consultation", id=c.id, description=f"Consultation, {doctor}", teeth=[], on=c.consult_date, amount_paise=int(amount))
            )

    billed = db.execute(
        select(Treatment, Service.name, func.sum(LedgerEntry.amount_paise))
        .join(Service, Service.id == Treatment.service_id)
        .join(LedgerEntry, LedgerEntry.treatment_id == Treatment.id)
        .where(
            Treatment.patient_id == patient_id,
            Treatment.status.in_(("ongoing", "finished")),
            LedgerEntry.kind.in_(("treatment_charge", "price_change")),
        )
        .group_by(Treatment.id, Service.name)
    ).all()
    for t, name, amount in billed:
        if t.id not in taken_treatments and amount and amount > 0:
            items.append(
                InvoiceableItem(kind="treatment", id=t.id, description=name, teeth=list(t.teeth), on=t.started_on, amount_paise=int(amount))
            )
    return sorted(items, key=lambda i: (i.on, i.kind != "consultation"))


def _outs(db: Session, clinic: Clinic, invoices: list[Invoice]) -> list[InvoiceOut]:
    if not invoices:
        return []
    lines: dict[uuid.UUID, list[InvoiceLine]] = {}
    for line in db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id.in_([i.id for i in invoices])).order_by(InvoiceLine.sort_order)):
        lines.setdefault(line.invoice_id, []).append(line)
    people = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_({i.issued_by for i in invoices})))}
    return [
        InvoiceOut(
            id=i.id, number_text=number_text(clinic, i), financial_year=i.financial_year, issued_on=i.issued_on,
            issued_by=people[i.issued_by], total_paise=i.total_paise, status=i.status, cancelled_at=i.cancelled_at,
            cancel_reason=i.cancel_reason,
            lines=[InvoiceLineOut(description=l.description, teeth=list(l.teeth), amount_paise=l.amount_paise) for l in lines.get(i.id, [])],
        )
        for i in invoices
    ]


def _invoices_out(db: Session, clinic: Clinic, patient_id: uuid.UUID) -> InvoicesOut:
    invoices = list(
        db.scalars(select(Invoice).where(Invoice.patient_id == patient_id).order_by(Invoice.issued_on.desc(), Invoice.created_at.desc()))
    )
    return InvoicesOut(invoices=_outs(db, clinic, invoices), invoiceable=invoiceable(db, patient_id))


@router.get("/patients/{patient_id}/invoices", response_model=InvoicesOut)
def list_invoices(patient_id: uuid.UUID, current: Current = Depends(require_admin)):
    _patient(current.db, patient_id)
    return _invoices_out(current.db, current.clinic, patient_id)


@router.post("/patients/{patient_id}/invoices", response_model=InvoicesOut, status_code=status.HTTP_201_CREATED)
def create_invoice(patient_id: uuid.UUID, payload: InvoiceCreate, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    wanted = {("consultation", i) for i in payload.consultation_ids} | {("treatment", i) for i in payload.treatment_ids}
    if not wanted:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Choose at least one item for the invoice.")
    available = {(item.kind, item.id): item for item in invoiceable(db, patient.id)}
    if not wanted <= set(available):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Some of these items are already on an invoice or not on the bill. Refresh and try again.",
        )
    day = payload.issued_on or clinic_today(clinic)
    if day > clinic_today(clinic):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The date cannot be in the future.")

    chosen = sorted((available[key] for key in wanted), key=lambda i: (i.on, i.kind != "consultation"))
    fy = financial_year(day)
    invoice = Invoice(
        clinic_id=clinic.id, patient_id=patient.id, number=take_next(db, clinic.id, "invoice", fy), financial_year=fy,
        issued_on=day, issued_by=current.staff.id, total_paise=sum(i.amount_paise for i in chosen),
    )
    db.add(invoice)
    db.flush()
    for order, item in enumerate(chosen):
        db.add(
            InvoiceLine(
                clinic_id=clinic.id, invoice_id=invoice.id, sort_order=order, description=item.description, teeth=item.teeth,
                amount_paise=item.amount_paise,
                consultation_id=item.id if item.kind == "consultation" else None,
                treatment_id=item.id if item.kind == "treatment" else None,
            )
        )
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="invoice", entity_id=invoice.id,
        patient_id=patient.id, changes={"number": number_text(clinic, invoice), "total_paise": invoice.total_paise, "items": len(chosen)},
        request=request,
    )
    db.commit()
    return _invoices_out(db, clinic, patient.id)


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoicesOut)
def cancel_invoice(invoice_id: uuid.UUID, payload: InvoiceCancel, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this invoice.")
    if invoice.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is already cancelled.")
    invoice.status, invoice.cancelled_at, invoice.cancelled_by, invoice.cancel_reason = "cancelled", now(), current.staff.id, payload.reason
    record(
        db, clinic_id=clinic.id, action="cancel", actor_id=current.staff.id, entity_type="invoice", entity_id=invoice.id,
        patient_id=invoice.patient_id, changes={"number": number_text(clinic, invoice), "reason": payload.reason}, request=request,
    )
    db.commit()
    return _invoices_out(db, clinic, invoice.patient_id)


@router.get("/invoices/{invoice_id}/pdf")
def invoice_pdf_file(invoice_id: uuid.UUID, request: Request, download: bool = False, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this invoice.")
    content, name = build_invoice_pdf(db, clinic, invoice)
    record(
        db, clinic_id=clinic.id, action="download" if download else "view", actor_id=current.staff.id, entity_type="invoice",
        entity_id=invoice.id, patient_id=invoice.patient_id,
        changes={"document": "invoice pdf", "number": number_text(clinic, invoice)}, request=request,
    )
    db.commit()
    return pdf_response(content, name, download)


def build_invoice_pdf(db: Session, clinic: Clinic, invoice: Invoice) -> tuple[bytes, str]:
    """The PDF and its file name. Also used for a link sent to the patient."""
    patient = db.get(Patient, invoice.patient_id)
    lines = db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order)).all()
    text = number_text(clinic, invoice)

    balance_note = None
    if invoice.status == "issued":
        paid, owed = db.execute(
            select(
                func.coalesce(func.sum(LedgerEntry.amount_paise).filter(LedgerEntry.kind.in_(("payment", "payment_reversal", "refund"))), 0),
                func.coalesce(func.sum(LedgerEntry.amount_paise), 0),
            ).where(LedgerEntry.patient_id == patient.id)
        ).one()
        today = date_text(clinic_today(clinic))
        if owed > 0:
            balance_note = f"Paid so far: {format_inr(-int(paid))}. Balance due on your account as of {today}: {format_inr(int(owed))}."
        elif owed < 0:
            balance_note = f"Paid so far: {format_inr(-int(paid))}. Credit on your account as of {today}: {format_inr(-int(owed))}."
        else:
            balance_note = f"Your account is fully paid as of {today}. Thank you."

    doc = InvoiceDoc(
        number_text=text, date_text=date_text(invoice.issued_on), patient_name=patient.name,
        patient_code=patient_code(clinic, patient.patient_number), phone=patient.phone,
        lines=[
            InvoiceLineDoc(
                description=l.description, teeth=", ".join(str(t) for t in l.teeth), amount=format_inr(l.amount_paise)
            )
            for l in lines
        ],
        total=format_inr(invoice.total_paise),
        cancelled_note=invoice.cancel_reason if invoice.status == "cancelled" else None,
        balance_note=balance_note,
    )
    return invoice_pdf(letterhead_for(clinic), doc), f"Invoice {text.replace('/', '-')}.pdf"