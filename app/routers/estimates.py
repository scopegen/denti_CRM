import uuid
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.billing import final_price, format_inr
from app.clinic_settings import get_settings
from app.clinic_time import clinic_today
from app.counters import take_next
from app.deps import Current, get_current, require_admin
from app.documents import date_text, doctor_for, letterhead_for, pdf_response
from app.models import Clinic, Estimate, EstimateItem, EstimateOption, Patient, Service, Staff, Treatment
from app.patients_util import patient_code
from app.pdf import EstimateDoc, EstimateOptionDoc, EstimateRowDoc, estimate_pdf
from app.routers.invoices import financial_year
from app.schemas import (
    EstimateAccept,
    EstimateCreate,
    EstimateItemOut,
    EstimateOptionOut,
    EstimateOut,
    PersonBrief,
)
from app.security import now

# Treatment estimates (quotes). The owner or receptionist gives one, with one or more options,
# built from the doctor's recommendations or planned treatments, or any service. Accepting an
# option turns its items into planned treatments at the quoted prices. Doctors see which
# estimates were given and accepted, never the amounts.

router = APIRouter(tags=["estimates"])


def _is_admin(staff: Staff) -> bool:
    return staff.role in ("owner", "admin")


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def number_text(estimate: Estimate) -> str:
    return f"EST/{estimate.financial_year}/{estimate.number:04d}"


def _discount(estimate: Estimate, subtotal: int) -> int:
    """The estimate's one discount, taken off an option's subtotal."""
    return subtotal - final_price(subtotal, None, 0, estimate.discount_type, estimate.discount_value)


def _status(clinic: Clinic, estimate: Estimate) -> str:
    if estimate.status == "given" and estimate.valid_until < clinic_today(clinic):
        return "expired"
    return estimate.status


def _loaded(db: Session, estimates: list[Estimate]):
    options: dict[uuid.UUID, list[EstimateOption]] = defaultdict(list)
    for o in db.scalars(select(EstimateOption).where(EstimateOption.estimate_id.in_([e.id for e in estimates])).order_by(EstimateOption.sort_order)):
        options[o.estimate_id].append(o)
    items: dict[uuid.UUID, list[EstimateItem]] = defaultdict(list)
    all_options = [o.id for os in options.values() for o in os]
    if all_options:
        for i in db.scalars(select(EstimateItem).where(EstimateItem.option_id.in_(all_options)).order_by(EstimateItem.sort_order)):
            items[i.option_id].append(i)
    return options, items


def _outs(db: Session, clinic: Clinic, estimates: list[Estimate], viewer: Staff) -> list[EstimateOut]:
    if not estimates:
        return []
    options, items = _loaded(db, estimates)
    doctors = {s.id: PersonBrief(id=s.id, name=s.name) for s in db.scalars(select(Staff).where(Staff.id.in_({e.doctor_id for e in estimates})))}
    money = _is_admin(viewer)
    out = []
    for e in estimates:
        option_outs = []
        for o in options[e.id]:
            subtotal = sum(i.unit_price_paise * i.quantity for i in items[o.id])
            discount = _discount(e, subtotal)
            option_outs.append(
                EstimateOptionOut(
                    id=o.id, label=o.label,
                    items=[
                        EstimateItemOut(
                            id=i.id, service_id=i.service_id, description=i.description, teeth=list(i.teeth), sittings=i.sittings,
                            quantity=i.quantity, unit_price_paise=i.unit_price_paise if money else None,
                            amount_paise=i.unit_price_paise * i.quantity if money else None,
                        )
                        for i in items[o.id]
                    ],
                    subtotal_paise=subtotal if money else None,
                    discount_paise=discount if money else None,
                    total_paise=subtotal - discount if money else None,
                )
            )
        out.append(
            EstimateOut(
                id=e.id, number_text=number_text(e), given_on=e.given_on, valid_until=e.valid_until, status=_status(clinic, e),
                doctor=doctors[e.doctor_id], notes=e.notes if money else None,
                discount_type=e.discount_type if money else None, discount_value=e.discount_value if money else None,
                options=option_outs, accepted_option_id=e.accepted_option_id, decided_at=e.decided_at,
            )
        )
    return out


def _estimate(db: Session, estimate_id: uuid.UUID) -> Estimate:
    estimate = db.get(Estimate, estimate_id)
    if estimate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this estimate.")
    return estimate


def _list(db: Session, clinic: Clinic, patient_id: uuid.UUID, viewer: Staff) -> list[EstimateOut]:
    rows = db.scalars(select(Estimate).where(Estimate.patient_id == patient_id).order_by(Estimate.given_on.desc(), Estimate.created_at.desc()))
    return _outs(db, clinic, list(rows), viewer)


@router.get("/patients/{patient_id}/estimates", response_model=list[EstimateOut])
def list_estimates(patient_id: uuid.UUID, current: Current = Depends(get_current)):
    _patient(current.db, patient_id)
    return _list(current.db, current.clinic, patient_id, current.staff)


@router.post("/patients/{patient_id}/estimates", response_model=list[EstimateOut], status_code=status.HTTP_201_CREATED)
def create_estimate(patient_id: uuid.UUID, payload: EstimateCreate, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    doctor = db.get(Staff, payload.doctor_id)
    if doctor is None or not doctor.treats_patients or doctor.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a doctor from this clinic.")
    all_items = [i for o in payload.options for i in o.items]
    if clinic.plan == "basic" and any(i.teeth for i in all_items):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Choosing teeth comes with the Standard and Pro plans.")
    services = {s.id: s for s in db.scalars(select(Service).where(Service.id.in_({i.service_id for i in all_items})))}
    if set(services) != {i.service_id for i in all_items}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose treatments from this clinic's services.")
    today = clinic_today(clinic)
    valid_until = payload.valid_until or today + timedelta(days=get_settings(db, clinic).estimate_valid_days)
    if valid_until < today:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Valid until cannot be before today.")
    if payload.discount_type == "amount":
        smallest = min(sum(i.unit_price_paise * i.quantity for i in o.items) for o in payload.options)
        if payload.discount_value > smallest:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The discount is more than an option's total.")

    fy = financial_year(today)
    estimate = Estimate(
        clinic_id=clinic.id, patient_id=patient.id, number=take_next(db, clinic.id, "estimate", fy), financial_year=fy,
        given_on=today, valid_until=valid_until, doctor_id=doctor.id, discount_type=payload.discount_type,
        discount_value=payload.discount_value, notes=payload.notes, created_by=current.staff.id,
    )
    db.add(estimate)
    db.flush()
    for order, option_in in enumerate(payload.options):
        option = EstimateOption(clinic_id=clinic.id, estimate_id=estimate.id, label=option_in.label, sort_order=order)
        db.add(option)
        db.flush()
        for item_order, item in enumerate(option_in.items):
            db.add(
                EstimateItem(
                    clinic_id=clinic.id, option_id=option.id, service_id=item.service_id,
                    description=item.description or services[item.service_id].name, teeth=item.teeth, sittings=item.sittings,
                    unit_price_paise=item.unit_price_paise, quantity=item.quantity, sort_order=item_order,
                )
            )
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="estimate", entity_id=estimate.id,
        patient_id=patient.id, changes={"number": number_text(estimate), "options": len(payload.options)}, request=request,
    )
    db.commit()
    return _list(db, clinic, patient.id, current.staff)


@router.post("/estimates/{estimate_id}/accept", response_model=list[EstimateOut])
def accept_estimate(estimate_id: uuid.UUID, payload: EstimateAccept, request: Request, current: Current = Depends(require_admin)):
    """The patient chose an option: its items become planned treatments at the quoted prices,
    with the estimate's discount shared across them."""
    db, clinic = current.db, current.clinic
    estimate = _estimate(db, estimate_id)
    if estimate.status != "given":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This estimate is already {estimate.status}.")
    option = db.get(EstimateOption, payload.option_id)
    if option is None or option.estimate_id != estimate.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose one of this estimate's options.")
    items = list(db.scalars(select(EstimateItem).where(EstimateItem.option_id == option.id).order_by(EstimateItem.sort_order)))
    subtotal = sum(i.unit_price_paise * i.quantity for i in items)
    discount = _discount(estimate, subtotal)

    shared = 0
    for n, item in enumerate(items):
        price = item.unit_price_paise * item.quantity
        # Each item takes its part of the discount; the last takes what rounding left over.
        share = discount - shared if n == len(items) - 1 else (discount * price // subtotal if subtotal else 0)
        shared += share
        db.add(
            Treatment(
                clinic_id=clinic.id, patient_id=estimate.patient_id, service_id=item.service_id, doctor_id=estimate.doctor_id,
                teeth=list(item.teeth), estimate_item_id=item.id, status="planned", price_paise=price,
                discount_type="amount" if share else None, discount_value=share, created_by=current.staff.id,
            )
        )
    estimate.status, estimate.accepted_option_id, estimate.decided_at, estimate.decided_by = "accepted", option.id, now(), current.staff.id
    record(
        db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="estimate", entity_id=estimate.id,
        patient_id=estimate.patient_id,
        changes={"status": ["given", "accepted"], "option": option.label, "treatments_planned": len(items), "total_paise": subtotal - discount},
        request=request,
    )
    db.commit()
    return _list(db, clinic, estimate.patient_id, current.staff)


@router.post("/estimates/{estimate_id}/decline", response_model=list[EstimateOut])
def decline_estimate(estimate_id: uuid.UUID, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    estimate = _estimate(db, estimate_id)
    if estimate.status != "given":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This estimate is already {estimate.status}.")
    estimate.status, estimate.decided_at, estimate.decided_by = "declined", now(), current.staff.id
    record(
        db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="estimate", entity_id=estimate.id,
        patient_id=estimate.patient_id, changes={"status": ["given", "declined"]}, request=request,
    )
    db.commit()
    return _list(db, clinic, estimate.patient_id, current.staff)


@router.get("/estimates/{estimate_id}/pdf")
def estimate_pdf_file(estimate_id: uuid.UUID, request: Request, download: bool = False, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    estimate = _estimate(db, estimate_id)
    content, name = build_estimate_pdf(db, clinic, estimate)
    record(
        db, clinic_id=clinic.id, action="download" if download else "view", actor_id=current.staff.id, entity_type="estimate",
        entity_id=estimate.id, patient_id=estimate.patient_id, changes={"document": "estimate pdf", "number": number_text(estimate)},
        request=request,
    )
    db.commit()
    return pdf_response(content, name, download)


def build_estimate_pdf(db: Session, clinic: Clinic, estimate: Estimate) -> tuple[bytes, str]:
    """The PDF and its file name. Also used for a link sent to the patient."""
    patient = db.get(Patient, estimate.patient_id)
    options, items = _loaded(db, [estimate])
    option_docs = []
    for o in options[estimate.id]:
        subtotal = sum(i.unit_price_paise * i.quantity for i in items[o.id])
        discount = _discount(estimate, subtotal)
        option_docs.append(
            EstimateOptionDoc(
                label=o.label,
                rows=[
                    EstimateRowDoc(
                        description=i.description if i.quantity == 1 else f"{i.description} x {i.quantity}",
                        teeth=", ".join(str(t) for t in i.teeth), sittings=str(i.sittings) if i.sittings else "",
                        amount=format_inr(i.unit_price_paise * i.quantity),
                    )
                    for i in items[o.id]
                ],
                subtotal=format_inr(subtotal), discount=format_inr(discount) if discount else None, total=format_inr(subtotal - discount),
            )
        )
    state = _status(clinic, estimate)
    status_note = {
        "accepted": f"ACCEPTED on {date_text(estimate.decided_at.astimezone(ZoneInfo(clinic.timezone)).date())}" if estimate.decided_at else "ACCEPTED",
        "declined": "DECLINED",
        "expired": f"EXPIRED on {date_text(estimate.valid_until)}",
    }.get(state)
    doc = EstimateDoc(
        number_text=number_text(estimate), date_text=date_text(estimate.given_on), valid_until_text=date_text(estimate.valid_until),
        patient_name=patient.name, patient_code=patient_code(clinic, patient.patient_number), phone=patient.phone,
        doctor=doctor_for(db, db.get(Staff, estimate.doctor_id)), options=option_docs, notes=estimate.notes, status_note=status_note,
    )
    return estimate_pdf(letterhead_for(clinic), doc), f"Estimate {number_text(estimate).replace('/', '-')}.pdf"