import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import consultation_final_fee, effective_at, final_price, format_inr, rebill_consultation, treatment_final_price
from app.clinic_time import clinic_today
from app.defaults import DEFAULT_PICKLISTS
from app.deps import Current, require_admin
from app.models import Clinic, Consultation, LedgerEntry, Patient, Picklist, Service, Staff, Treatment
from app.patients_util import normalise_phone, parse_patient_number, patient_code
from app.routers.invoices import issued_invoice_numbers, refuse_if_invoiced
from app.schemas import (
    BillOut,
    ChargeOut,
    DueRow,
    DuesOut,
    LedgerOut,
    OpeningBalanceIn,
    PatientBrief,
    PaymentIn,
    PersonBrief,
    RefundIn,
    ReversalIn,
    TreatmentPriceIn,
)
from app.security import now

# The patient's bill and the clinic's dues: for the owner and receptionist only. The bill is
# ledger entries that are only ever added (app/billing.py). A wrong payment is reversed with
# a reason and the right one added; nothing already on the bill changes.

router = APIRouter(tags=["billing"])

CHARGES = ("consultation_fee", "treatment_charge", "price_change", "opening_balance")
MONEY_IN = ("payment", "payment_reversal", "refund")


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def _payment_modes(db: Session) -> list[str]:
    modes = list(
        db.scalars(
            select(Picklist.value)
            .where(Picklist.list == "payment_mode", Picklist.active.is_(True))
            .order_by(Picklist.sort_order, Picklist.value)
        )
    )
    # A clinic with no modes of its own can still take payments.
    return modes or DEFAULT_PICKLISTS["payment_mode"]


def _check_mode(db: Session, mode: str) -> str:
    mode = mode.strip()
    if mode not in _payment_modes(db):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a payment mode from the clinic's list.")
    return mode


def _teeth_text(teeth: list[int]) -> str:
    return f"tooth {teeth[0]}" if len(teeth) == 1 else f"teeth {', '.join(str(t) for t in teeth)}"


def _describe(entry: LedgerEntry, treatments: dict[uuid.UUID, tuple[str, list[int]]]) -> str:
    treatment = treatments.get(entry.treatment_id) if entry.treatment_id else None
    what = f"{treatment[0]}, {_teeth_text(treatment[1])}" if treatment and treatment[1] else treatment[0] if treatment else None
    if entry.kind == "consultation_fee":
        return "Consultation fee"
    if entry.kind == "treatment_charge":
        return what or "Treatment"
    if entry.kind == "price_change":
        if entry.consultation_id:
            return f"Consultation fee: {entry.note}" if entry.note else "Consultation fee: changed"
        return f"{what or 'Treatment'}: {entry.note}" if entry.note else f"{what or 'Treatment'}: price changed"
    if entry.kind == "opening_balance":
        return "Opening balance" if entry.amount_paise > 0 else "Opening credit"
    if entry.kind == "payment":
        return f"Payment, {entry.payment_mode}"
    if entry.kind == "payment_reversal":
        return "Payment reversed"
    return f"Refund, {entry.payment_mode}"


def bill_for(db: Session, clinic: Clinic, patient_id: uuid.UUID) -> BillOut:
    entries = list(
        db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.patient_id == patient_id)
            .order_by(LedgerEntry.effective_at, LedgerEntry.created_at, LedgerEntry.id)
        )
    )
    treatments = {
        t.id: (name, list(t.teeth))
        for t, name in db.execute(
            select(Treatment, Service.name)
            .join(Service, Service.id == Treatment.service_id)
            .where(Treatment.id.in_({e.treatment_id for e in entries if e.treatment_id}))
        )
    }
    people = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_({e.created_by for e in entries})))}
    reversed_ids = {e.reverses_entry_id for e in entries if e.kind == "payment_reversal"}

    balance = 0
    lines = []
    for e in entries:
        balance += e.amount_paise
        lines.append(
            LedgerOut(
                id=e.id, kind=e.kind, amount_paise=e.amount_paise, effective_at=e.effective_at,
                description=_describe(e, treatments), payment_mode=e.payment_mode, note=e.note,
                consultation_id=e.consultation_id, treatment_id=e.treatment_id, reverses_entry_id=e.reverses_entry_id,
                reversed=e.id in reversed_ids, created_by=people[e.created_by], created_at=e.created_at, balance_paise=balance,
            )
        )
    charged = sum(e.amount_paise for e in entries if e.kind in CHARGES)
    return BillOut(
        outstanding_paise=balance,
        charged_paise=charged,
        paid_paise=charged - balance,
        entries=lines,
        charges=_charges(db, clinic, patient_id, entries, treatments),
        has_opening_balance=any(e.kind == "opening_balance" for e in entries),
        payment_modes=_payment_modes(db),
    )


def _charges(
    db: Session, clinic: Clinic, patient_id: uuid.UUID, entries: list[LedgerEntry], treatments: dict[uuid.UUID, tuple[str, list[int]]]
) -> list[ChargeOut]:
    """Each consultation fee and started treatment on the bill, with its price, increase,
    discount and what the bill holds for it. Cancelled treatments are off the bill."""
    billed: dict[uuid.UUID, int] = {}
    for e in entries:
        if e.kind in ("consultation_fee", "treatment_charge", "price_change"):
            key = e.consultation_id or e.treatment_id
            if key:
                billed[key] = billed.get(key, 0) + e.amount_paise
    invoiced = issued_invoice_numbers(db, clinic, patient_id)
    charges: list[ChargeOut] = []

    consultation_ids = {e.consultation_id for e in entries if e.consultation_id}
    if consultation_ids:
        rows = db.execute(
            select(Consultation, Staff.name).join(Staff, Staff.id == Consultation.doctor_id).where(Consultation.id.in_(consultation_ids))
        ).all()
        for c, doctor in rows:
            charges.append(
                ChargeOut(
                    kind="consultation", id=c.id, description=f"Consultation fee, {doctor}", on=c.consult_date,
                    price_paise=c.fee_paise, adjustment_type=c.fee_adjustment_type, adjustment_value=c.fee_adjustment_value,
                    discount_type=c.fee_discount_type, discount_value=c.fee_discount_value, final_paise=consultation_final_fee(c),
                    billed_paise=billed.get(c.id, 0), invoice_number=invoiced.get(c.id),
                )
            )
    if treatments:
        for t in db.scalars(select(Treatment).where(Treatment.id.in_(treatments.keys()), Treatment.status.in_(("ongoing", "finished")))):
            name, teeth = treatments[t.id]
            charges.append(
                ChargeOut(
                    kind="treatment", id=t.id, description=f"{name}, {_teeth_text(teeth)}" if teeth else name, on=t.started_on,
                    price_paise=t.price_paise, adjustment_type=t.adjustment_type, adjustment_value=t.adjustment_value,
                    discount_type=t.discount_type, discount_value=t.discount_value, final_paise=treatment_final_price(t),
                    billed_paise=billed.get(t.id, 0), invoice_number=invoiced.get(t.id),
                )
            )
    return sorted(charges, key=lambda c: (c.on or date.min, c.kind != "consultation"))


def _outstanding(db: Session, patient_id: uuid.UUID) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(LedgerEntry.patient_id == patient_id)))


def _day(clinic: Clinic, value: date | None) -> date:
    day = value or clinic_today(clinic)
    if day > clinic_today(clinic):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The date cannot be in the future.")
    return day


def _add(db: Session, current: Current, request: Request, patient: Patient, kind: str, amount: int, when, **fields) -> LedgerEntry:
    entry = LedgerEntry(
        clinic_id=current.clinic.id, patient_id=patient.id, kind=kind, amount_paise=amount, effective_at=when,
        created_by=current.staff.id, **fields,
    )
    db.add(entry)
    db.flush()
    changes = {"kind": kind, "amount_paise": amount, "payment_mode": fields.get("payment_mode"), "note": fields.get("note")}
    record(
        db, clinic_id=current.clinic.id, action="create", actor_id=current.staff.id, entity_type="ledger_entry",
        entity_id=entry.id, patient_id=patient.id, changes=changes, request=request,
    )
    return entry


@router.get("/patients/{patient_id}/bill", response_model=BillOut)
def get_bill(patient_id: uuid.UUID, current: Current = Depends(require_admin)):
    _patient(current.db, patient_id)
    return bill_for(current.db, current.clinic, patient_id)


@router.put("/consultations/{consultation_id}/fee", response_model=BillOut)
def set_consultation_fee(consultation_id: uuid.UUID, payload: TreatmentPriceIn, request: Request, current: Current = Depends(require_admin)):
    """A consultation's fee, increase and discount, changed from the bill (a free review visit, a
    discount). The bill gets only the difference; what was billed before stays as it was."""
    db, clinic = current.db, current.clinic
    c = db.get(Consultation, consultation_id)
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this consultation.")
    refuse_if_invoiced(db, clinic, c.patient_id, c.id)
    if payload.discount_type == "amount":
        increased = final_price(payload.price_paise, payload.adjustment_type, payload.adjustment_value, None, 0)
        if payload.discount_value > increased:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The discount is more than the fee.")
    wanted = {
        "fee_paise": payload.price_paise, "fee_adjustment_type": payload.adjustment_type,
        "fee_adjustment_value": payload.adjustment_value, "fee_discount_type": payload.discount_type,
        "fee_discount_value": payload.discount_value,
    }
    changes = {field: [getattr(c, field), value] for field, value in wanted.items() if getattr(c, field) != value}
    if changes:
        before = consultation_final_fee(c)
        for field, value in wanted.items():
            setattr(c, field, value)
        c.updated_at = now()
        changes["final_fee_paise"] = [before, consultation_final_fee(c)]
        rebill_consultation(db, clinic, c, current.staff.id)
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="consultation",
            entity_id=c.id, patient_id=c.patient_id, changes=changes, request=request,
        )
        db.commit()
    return bill_for(db, clinic, c.patient_id)


@router.post("/patients/{patient_id}/payments", response_model=BillOut, status_code=status.HTTP_201_CREATED)
def take_payment(patient_id: uuid.UUID, payload: PaymentIn, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    mode = _check_mode(db, payload.payment_mode)
    day = _day(clinic, payload.paid_on)
    _add(db, current, request, patient, "payment", -payload.amount_paise, effective_at(clinic, day), payment_mode=mode, note=payload.note)
    db.commit()
    return bill_for(db, clinic, patient.id)


@router.post("/ledger/{entry_id}/reverse", response_model=BillOut)
def reverse_payment(entry_id: uuid.UUID, payload: ReversalIn, request: Request, current: Current = Depends(require_admin)):
    """For a payment entered wrongly: it stays on the bill, and a reversal cancels it out."""
    db, clinic = current.db, current.clinic
    payment = db.get(LedgerEntry, entry_id)
    if payment is None or payment.kind != "payment":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this payment.")
    already = db.scalar(select(LedgerEntry.id).where(LedgerEntry.reverses_entry_id == payment.id))
    if already is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment has already been reversed.")
    patient = _patient(db, payment.patient_id)
    _add(
        db, current, request, patient, "payment_reversal", -payment.amount_paise, effective_at(clinic, clinic_today(clinic)),
        payment_mode=payment.payment_mode, note=payload.reason, reverses_entry_id=payment.id,
    )
    db.commit()
    return bill_for(db, clinic, patient.id)


@router.post("/patients/{patient_id}/refunds", response_model=BillOut, status_code=status.HTTP_201_CREATED)
def give_refund(patient_id: uuid.UUID, payload: RefundIn, request: Request, current: Current = Depends(require_admin)):
    """Money handed back from the patient's credit. Never more than the credit."""
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    mode = _check_mode(db, payload.payment_mode)
    credit = -_outstanding(db, patient.id)
    if payload.amount_paise > credit:
        detail = f"The patient's credit is {format_inr(credit)}." if credit > 0 else "The patient has no credit to refund."
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    _add(db, current, request, patient, "refund", payload.amount_paise, effective_at(clinic, clinic_today(clinic)), payment_mode=mode, note=payload.note)
    db.commit()
    return bill_for(db, clinic, patient.id)


@router.post("/patients/{patient_id}/opening-balance", response_model=BillOut, status_code=status.HTTP_201_CREATED)
def opening_balance(patient_id: uuid.UUID, payload: OpeningBalanceIn, request: Request, current: Current = Depends(require_admin)):
    """What the patient owed, or had in credit, in the clinic's old system. Once per patient."""
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    exists = db.scalar(select(LedgerEntry.id).where(LedgerEntry.patient_id == patient.id, LedgerEntry.kind == "opening_balance"))
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This patient already has an opening balance.")
    _add(db, current, request, patient, "opening_balance", payload.amount_paise, effective_at(clinic, clinic_today(clinic)), note=payload.note)
    db.commit()
    return bill_for(db, clinic, patient.id)


@router.get("/dues", response_model=DuesOut)
def dues(
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current: Current = Depends(require_admin),
):
    """Everyone who owes the clinic money, most owed first."""
    db, clinic = current.db, current.clinic
    owed = func.sum(LedgerEntry.amount_paise)
    totals = (
        select(
            LedgerEntry.patient_id.label("patient_id"),
            owed.label("owed"),
            func.max(case((LedgerEntry.kind == "payment", LedgerEntry.effective_at))).label("last_payment_at"),
            func.max(case((LedgerEntry.kind.in_(CHARGES), LedgerEntry.effective_at))).label("last_charge_at"),
        )
        .group_by(LedgerEntry.patient_id)
        .having(owed > 0)
        .subquery()
    )
    stmt = (
        select(Patient, totals.c.owed, totals.c.last_payment_at, totals.c.last_charge_at)
        .join(totals, totals.c.patient_id == Patient.id)
        .where(Patient.merged_into_id.is_(None))
    )
    if q and q.strip():
        term = q.strip()
        matches = [Patient.name.ilike(f"%{term}%"), Patient.phone.ilike(f"%{normalise_phone(term)}%")]
        number = parse_patient_number(term)
        if number is not None:
            matches.append(Patient.patient_number == number)
        stmt = stmt.where(or_(*matches))

    filtered = stmt.subquery()
    count, total = db.execute(select(func.count(), func.coalesce(func.sum(filtered.c.owed), 0)).select_from(filtered)).one()
    rows = db.execute(stmt.order_by(totals.c.owed.desc(), Patient.name).offset((page - 1) * page_size).limit(page_size)).all()
    return DuesOut(
        total_paise=int(total),
        count=count,
        page=page,
        page_size=page_size,
        items=[
            DueRow(
                patient=PatientBrief(
                    id=p.id, code=patient_code(clinic, p.patient_number), name=p.name, phone=p.phone,
                    medical_conditions=list(p.medical_conditions or []),
                ),
                outstanding_paise=int(owed), last_payment_at=last_payment_at, last_charge_at=last_charge_at,
            )
            for p, owed, last_payment_at, last_charge_at in rows
        ],
    )
