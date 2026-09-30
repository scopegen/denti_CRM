import base64
import binascii
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.audit import record
from app.clinic_time import clinic_today
from app.deps import Current, require_admin
from app.files_service import read_file, store_file
from app.models import Clinic, Counter, File, ImportBatch, LedgerEntry, Patient, Picklist, Staff
from app.patient_import import FIELD_KEYS, FIELDS, ImportFileError, RowCheck, check_rows, read_table, suggest_mapping
from app.patients_util import parse_patient_number, patient_code
from app.plans import PLAN_NAMES, PLANS
from app.schemas import (
    ImportBatchOut,
    ImportCheckOut,
    ImportField,
    ImportMappingIn,
    ImportPreview,
    ImportRowOut,
    ImportRun,
    ImportUpload,
    PersonBrief,
    PlanFit,
)
from app.security import now

# Settings, Import patients: for the owner and receptionist. Upload an Excel or CSV file,
# confirm which column is which, review the flagged rows, then import. Imported patients count
# towards the plan's limit. An import can be undone for 30 minutes, only while nothing has been
# recorded against its patients; that is the only way patients are ever removed.

router = APIRouter(prefix="/imports", tags=["patient import"])

MAX_FILE_BYTES = 5 * 1024 * 1024
UNDO_MINUTES = 30
_CONTENT_TYPES = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv"}
_ID_PREFIX = re.compile(r"^\s*([A-Za-z]+)[-/ ]?\d")


def _import_file(db: Session, file_id: uuid.UUID) -> File:
    found = db.get(File, file_id)
    if found is None or found.kind != "import":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this file. Upload it again.")
    return found


def _table(found: File):
    try:
        return read_table(read_file(found), found.title or "file.csv")
    except ImportFileError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))


def _checks(db: Session, clinic: Clinic, rows, mapping: dict[str, int | None]) -> list[RowCheck]:
    unknown = set(mapping) - set(FIELD_KEYS)
    if unknown:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Unknown field: {sorted(unknown)[0]}")
    if mapping.get("name") is None or mapping.get("phone") is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Choose the columns for name and phone.")
    existing = {
        (p.phone, p.name.lower()): patient_code(clinic, p.patient_number)
        for p in db.scalars(select(Patient).where(Patient.merged_into_id.is_(None)))
    }
    conditions = list(db.scalars(select(Picklist.value).where(Picklist.list == "medical_condition", Picklist.active.is_(True))))
    return check_rows(rows, mapping, existing=existing, known_conditions=conditions, today=clinic_today(clinic))


def _plan_fit(db: Session, clinic: Clinic, adding: int) -> PlanFit:
    used = db.scalar(select(func.count()).select_from(Patient))
    limit = PLANS[clinic.plan].patients
    fits = limit is None or used + adding <= limit
    suggested = None
    if not fits:
        suggested = next(
            (key for key, plan in PLANS.items() if plan.patients is None or used + adding <= plan.patients), None
        )
    return PlanFit(
        used=used, adding=adding, limit=limit, fits=fits, suggested_plan=suggested,
        suggested_plan_name=PLAN_NAMES[suggested] if suggested else None,
    )


def _kept_numbers(db: Session, rows: list[RowCheck]) -> tuple[dict[int, int] | None, str | None]:
    """The patient number each row keeps, when the old IDs allow it; otherwise why not."""
    numbers: dict[int, int] = {}
    prefixes = set()
    for r in rows:
        number = parse_patient_number(r.previous_id or "")
        if number is None:
            return None, f"Row {r.row} has no old patient ID that ends in a number."
        match = _ID_PREFIX.match(r.previous_id or "")
        prefixes.add(match.group(1).upper() if match else "")
        numbers[r.row] = number
    if len(prefixes) > 1:
        return None, "The old IDs do not all share one prefix."
    if len(set(numbers.values())) != len(numbers):
        return None, "Two patients in the file have the same old ID number."
    taken = set(db.scalars(select(Patient.patient_number).where(Patient.patient_number.in_(set(numbers.values())))))
    if taken:
        return None, f"Patient number {min(taken)} is already used in Denti."
    return numbers, None


def _row_out(r: RowCheck) -> ImportRowOut:
    return ImportRowOut(row=r.row, name=r.name, phone=r.phone, previous_id=r.previous_id, kind=r.kind, problems=r.problems, warnings=r.warnings)


def _batch_out(db: Session, batch: ImportBatch) -> ImportBatchOut:
    by = db.get(Staff, batch.uploaded_by)
    return ImportBatchOut(
        id=batch.id, file_name=batch.file_name, rows_total=batch.rows_total, rows_imported=batch.rows_imported,
        rows_skipped=batch.rows_skipped, kept_numbers=batch.kept_numbers, uploaded_by=PersonBrief(id=by.id, name=by.name),
        created_at=batch.created_at, undo_deadline=batch.undo_deadline, undone_at=batch.undone_at,
        can_undo=batch.undone_at is None and now() < batch.undo_deadline,
    )


@router.post("/files", response_model=ImportPreview, status_code=status.HTTP_201_CREATED)
def upload(payload: ImportUpload, current: Current = Depends(require_admin)):
    """Stores the file and reads its headings, with a suggested match for each field."""
    db, clinic = current.db, current.clinic
    extension = next((ext for ext in _CONTENT_TYPES if payload.file_name.lower().endswith(ext)), None)
    if extension is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Upload an Excel file (.xlsx) or a CSV file.")
    try:
        data = base64.b64decode(payload.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That file could not be read.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The file can be up to 5 MB.")
    try:
        headers, rows = read_table(data, payload.file_name)
    except ImportFileError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))

    stored = store_file(
        db, clinic_id=clinic.id, kind="import", data=data, content_type=_CONTENT_TYPES[extension], uploaded_by=current.staff.id,
    )
    stored.title = payload.file_name
    db.commit()
    return ImportPreview(
        file_id=stored.id, file_name=payload.file_name, columns=headers,
        fields=[ImportField(key=key, label=label) for key, label, _ in FIELDS], mapping=suggest_mapping(headers),
        sample=[["" if c is None else str(c) for c in r] for r in rows[:5]], row_count=len(rows),
    )


@router.post("/check", response_model=ImportCheckOut)
def check(payload: ImportMappingIn, current: Current = Depends(require_admin)):
    """What would happen: which rows import, which are skipped and why, and whether the plan has room."""
    db, clinic = current.db, current.clinic
    _, rows = _table(_import_file(db, payload.file_id))
    checks = _checks(db, clinic, rows, payload.mapping)
    ok = [c for c in checks if c.kind == "ok"]
    _, reason = _kept_numbers(db, ok) if ok else (None, "There are no patients to import.")
    return ImportCheckOut(
        ok=len(ok),
        duplicates=sum(c.kind == "duplicate" for c in checks),
        invalid=sum(c.kind == "invalid" for c in checks),
        flagged=[_row_out(c) for c in checks if c.kind != "ok" or c.warnings],
        plan=_plan_fit(db, clinic, len(ok)),
        keep_numbers_possible=reason is None,
        keep_numbers_reason=reason,
    )


@router.post("", response_model=ImportBatchOut, status_code=status.HTTP_201_CREATED)
def run_import(payload: ImportRun, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    found = _import_file(db, payload.file_id)
    _, rows = _table(found)
    checks = _checks(db, clinic, rows, payload.mapping)
    chosen = [c for c in checks if c.kind == "ok" or (c.kind == "duplicate" and c.row in set(payload.include_rows))]
    if not chosen:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="There are no patients to import.")

    fit = _plan_fit(db, clinic, len(chosen))
    if not fit.fits:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "plan_limit",
                "message": f"Your {PLAN_NAMES[clinic.plan]} plan includes {fit.limit:,} patients; this import would take you to "
                f"{fit.used + fit.adding:,}. The {fit.suggested_plan_name} plan fits.",
            },
        )
    kept = None
    if payload.keep_numbers:
        kept, reason = _kept_numbers(db, chosen)
        if kept is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"The old numbers cannot be kept: {reason}")

    batch = ImportBatch(
        clinic_id=clinic.id, file_id=found.id, file_name=found.title or "Patients", uploaded_by=current.staff.id,
        rows_total=len(checks), rows_imported=len(chosen), rows_skipped=len(checks) - len(chosen), kept_numbers=kept is not None,
        undo_deadline=now() + timedelta(minutes=UNDO_MINUTES),
    )
    db.add(batch)
    db.flush()

    counter_row = (
        insert(Counter).values(clinic_id=clinic.id, kind="patient", period="all", next_value=1)
        .on_conflict_do_nothing(index_elements=["clinic_id", "kind", "period"])
    )
    db.execute(counter_row)
    counter = db.execute(
        select(Counter).where(Counter.clinic_id == clinic.id, Counter.kind == "patient", Counter.period == "all").with_for_update()
    ).scalar_one()
    if kept:
        # New patients registered later continue from the highest kept number.
        counter.next_value = max(counter.next_value, max(kept.values()) + 1)

    for c in chosen:
        values = c.values
        if kept:
            number = kept[c.row]
        else:
            number = counter.next_value
            counter.next_value += 1
        patient = Patient(
            clinic_id=clinic.id, patient_number=number, previous_id=values["previous_id"], name=values["name"],
            phone=values["phone"], email=values["email"], gender=values["gender"], dob=values["dob"],
            birth_year=values["birth_year"], city=values["city"], area=values["area"],
            medical_conditions=values["medical_conditions"], medical_history=values["medical_history"],
            registered_by=current.staff.id, import_batch_id=batch.id,
        )
        db.add(patient)
        db.flush()
        if values["opening_balance"]:
            db.add(
                LedgerEntry(
                    clinic_id=clinic.id, patient_id=patient.id, kind="opening_balance", amount_paise=values["opening_balance"],
                    effective_at=now(), note="Imported", created_by=current.staff.id,
                )
            )
        record(
            db, clinic_id=clinic.id, action="import", actor_id=current.staff.id, entity_type="patient", entity_id=patient.id,
            patient_id=patient.id, changes={"name": patient.name, "phone": patient.phone, "number": number, "batch": str(batch.id)},
            request=request,
        )
    record(
        db, clinic_id=clinic.id, action="import", actor_id=current.staff.id, entity_type="import_batch", entity_id=batch.id,
        changes={"file": batch.file_name, "imported": batch.rows_imported, "skipped": batch.rows_skipped, "kept_numbers": batch.kept_numbers},
        request=request,
    )
    db.commit()
    return _batch_out(db, batch)


@router.get("", response_model=list[ImportBatchOut])
def recent_imports(current: Current = Depends(require_admin)):
    db = current.db
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(20))
    return [_batch_out(db, b) for b in batches]


_UNDO_ERRORS = {
    "DN404": (status.HTTP_404_NOT_FOUND, "We could not find this import."),
    "DN409": (status.HTTP_409_CONFLICT, "This import has already been undone."),
    "DN410": (status.HTTP_409_CONFLICT, "The 30 minutes to undo this import are over."),
    "DN423": (
        status.HTTP_409_CONFLICT,
        "Something has already been recorded for some of these patients, so the import cannot be undone.",
    ),
}


@router.post("/{batch_id}/undo", response_model=ImportBatchOut)
def undo_import(batch_id: uuid.UUID, request: Request, current: Current = Depends(require_admin)):
    """Removes the import's patients and their opening balances, within 30 minutes and only
    while nothing else has been recorded for them. The database function does the checks."""
    db, clinic = current.db, current.clinic
    try:
        removed = db.execute(text("SELECT undo_patient_import(:batch, :by)"), {"batch": batch_id, "by": current.staff.id}).scalar_one()
    except DBAPIError as error:
        db.rollback()
        code, message = _UNDO_ERRORS.get(getattr(error.orig, "sqlstate", ""), (None, None))
        if code is None:
            raise
        raise HTTPException(status_code=code, detail=message)
    record(
        db, clinic_id=clinic.id, action="undo", actor_id=current.staff.id, entity_type="import_batch", entity_id=batch_id,
        changes={"patients_removed": removed}, request=request,
    )
    db.commit()
    return _batch_out(db, db.get(ImportBatch, batch_id))
