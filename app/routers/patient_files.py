import re
import uuid
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.config import settings
from app.db import get_db, use_clinic
from app.deps import Current, get_current
from app.documents import date_text
from app.files_service import (
    MAX_PATIENT_FILE_BYTES,
    PATIENT_FILE_KINDS,
    PATIENT_FILE_TYPES,
    NotAnImage,
    UploadGrant,
    ViewGrant,
    make_thumbnail,
    new_patient_file_key,
    read_upload_token,
    read_view_token,
    storage_used,
    upload_token,
    upload_url,
    view_url,
)
from app.models import Clinic, File, Patient, Staff, Subscription
from app.patients_util import patient_code
from app.plans import STORAGE_PACK_GB, storage_limit_bytes
from app.schemas import PatientFileCreate, PatientFileDetails, PatientFileOut, PersonBrief, UploadRequest, UploadTicket
from app.security import now
from app.storage import get_storage

# X-rays, photos and documents on a patient's record. Everyone who treats or registers
# patients can add and view them. Uploading takes three steps, the same as S3 will:
#   1. POST /patients/{id}/files/uploads  checks the type, size and storage room, returns a link
#   2. PUT the bytes to that link          (locally this API; live, S3)
#   3. POST /patients/{id}/files           checks what arrived, makes a thumbnail, records it
# Deleting moves a file to the bin for 30 days; it can be restored until then.

router = APIRouter(tags=["patient files"])

BIN_DAYS = 30
KIND_LABEL = {"xray": "X-ray", "photo": "Photo", "document": "Document"}


def _patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None or patient.merged_into_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this patient.")
    return patient


def _file(db: Session, file_id: uuid.UUID) -> File:
    found = db.get(File, file_id)
    if found is None or found.patient_id is None or found.kind not in PATIENT_FILE_KINDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this file.")
    return found


def _check_teeth_allowed(clinic: Clinic, teeth: list[int]) -> None:
    if teeth and clinic.plan == "basic":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tagging teeth comes with the Standard and Pro plans."
        )


def _check_room(db: Session, clinic: Clinic, extra_bytes: int) -> None:
    subscription = db.get(Subscription, clinic.id)
    limit = storage_limit_bytes(clinic.plan, subscription.storage_addon_packs if subscription else 0)
    if storage_used(db) + extra_bytes > limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "storage_limit",
                "message": f"Your storage is full. Add a {STORAGE_PACK_GB} GB storage pack or upgrade your plan to upload more.",
            },
        )


def _thumbnail_key(storage_key: str) -> str:
    return f"{storage_key.rsplit('.', 1)[0]}_thumb.jpg"


def _outs(db: Session, clinic: Clinic, files: list[File], viewer: Staff) -> list[PatientFileOut]:
    names = {s.id: s.name for s in db.scalars(select(Staff).where(Staff.id.in_({f.uploaded_by for f in files})))}
    return [
        PatientFileOut(
            id=f.id,
            kind=f.kind,
            title=f.title,
            teeth=list(f.teeth),
            content_type=f.content_type,
            size_bytes=f.size_bytes,
            width=f.width,
            height=f.height,
            uploaded_by=PersonBrief(id=f.uploaded_by, name=names.get(f.uploaded_by, "Unknown")),
            uploaded_at=f.uploaded_at,
            deleted_at=f.deleted_at,
            purge_after=f.purge_after,
            view_url=view_url(ViewGrant(clinic.id, f.id, thumbnail=False, download=False)),
            download_url=view_url(ViewGrant(clinic.id, f.id, thumbnail=False, download=True, staff_id=viewer.id)),
            thumbnail_url=view_url(ViewGrant(clinic.id, f.id, thumbnail=True, download=False)) if f.thumbnail_key else None,
        )
        for f in files
    ]


@router.post("/patients/{patient_id}/files/uploads", response_model=UploadTicket)
def start_upload(patient_id: uuid.UUID, payload: UploadRequest, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    if payload.content_type not in PATIENT_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Upload a JPG, PNG or WebP image, or a PDF."
        )
    if payload.size_bytes > MAX_PATIENT_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Each file can be up to 25 MB.")
    _check_room(db, clinic, payload.size_bytes)

    grant = UploadGrant(
        clinic_id=clinic.id, patient_id=patient.id, storage_key=new_patient_file_key(clinic.id, payload.content_type),
        content_type=payload.content_type, max_bytes=payload.size_bytes,
    )
    token = upload_token(grant)
    return UploadTicket(upload_token=token, upload_url=upload_url(token), headers={"Content-Type": payload.content_type})


@router.put("/uploads/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_upload(token: str, request: Request, db: Session = Depends(get_db)):
    """Local storage only: stands in for S3's signed upload link. The token is the permission."""
    if settings.storage_backend != "local":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    grant = read_upload_token(token)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This upload link has expired. Upload the file again.")
    if request.headers.get("content-type") != grant.content_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file type does not match the upload link.")

    use_clinic(db, grant.clinic_id)
    if db.scalar(select(File.id).where(File.storage_key == grant.storage_key)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This file is already saved.")

    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > grant.max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is bigger than announced.")
        chunks.append(chunk)
    get_storage().put(grant.storage_key, b"".join(chunks), grant.content_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/patients/{patient_id}/files", response_model=PatientFileOut, status_code=status.HTTP_201_CREATED)
def confirm_upload(patient_id: uuid.UUID, payload: PatientFileCreate, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    patient = _patient(db, patient_id)
    grant = read_upload_token(payload.upload_token)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This upload has expired. Upload the file again.")
    if grant.clinic_id != clinic.id or grant.patient_id != patient.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This upload belongs to another patient.")
    if db.scalar(select(File.id).where(File.storage_key == grant.storage_key)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This file is already saved.")
    _check_teeth_allowed(clinic, payload.teeth)

    storage = get_storage()
    size = storage.size(grant.storage_key)
    if size is None or size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file did not arrive. Upload it again.")
    if size > grant.max_bytes:
        storage.delete(grant.storage_key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is bigger than announced. Upload it again.")
    _check_room(db, clinic, size)

    data = storage.get(grant.storage_key)
    width = height = thumbnail_key = None
    if grant.content_type.startswith("image/"):
        try:
            thumbnail, width, height = make_thumbnail(data)
        except NotAnImage as error:
            storage.delete(grant.storage_key)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))
        thumbnail_key = _thumbnail_key(grant.storage_key)
        storage.put(thumbnail_key, thumbnail, "image/jpeg")
    elif not data.startswith(b"%PDF"):
        storage.delete(grant.storage_key)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That file is not a readable PDF.")

    saved = File(
        clinic_id=clinic.id, patient_id=patient.id, kind=payload.kind, storage_key=grant.storage_key,
        content_type=grant.content_type, size_bytes=size, width=width, height=height, thumbnail_key=thumbnail_key,
        teeth=payload.teeth, title=payload.title, uploaded_by=current.staff.id,
    )
    db.add(saved)
    db.flush()
    record(
        db, clinic_id=clinic.id, action="create", actor_id=current.staff.id, entity_type="file", entity_id=saved.id,
        patient_id=patient.id,
        changes={"kind": saved.kind, "title": saved.title, "teeth": saved.teeth, "size_bytes": size},
        request=request,
    )
    db.commit()
    return _outs(db, clinic, [saved], current.staff)[0]


@router.get("/patients/{patient_id}/files", response_model=list[PatientFileOut])
def list_files(patient_id: uuid.UUID, bin: bool = False, current: Current = Depends(get_current)):
    """The patient's files, newest first. With bin=true, the ones in the bin instead."""
    db = current.db
    _patient(db, patient_id)
    in_bin = File.deleted_at.is_not(None) if bin else File.deleted_at.is_(None)
    rows = db.scalars(
        select(File)
        .where(File.patient_id == patient_id, File.kind.in_(PATIENT_FILE_KINDS), in_bin)
        .order_by(File.uploaded_at.desc())
    ).all()
    return _outs(db, current.clinic, list(rows), current.staff)


@router.patch("/files/{file_id}", response_model=PatientFileOut)
def update_file(file_id: uuid.UUID, payload: PatientFileDetails, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    found = _file(db, file_id)
    if found.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Restore this file from the bin to change it.")
    if payload.teeth != list(found.teeth):
        _check_teeth_allowed(clinic, payload.teeth)
    changes = {}
    for field in ("kind", "title", "teeth"):
        old, new = getattr(found, field), getattr(payload, field)
        old = list(old) if field == "teeth" else old
        if old != new:
            changes[field] = [old, new]
            setattr(found, field, new)
    if changes:
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="file", entity_id=found.id,
            patient_id=found.patient_id, changes=changes, request=request,
        )
        db.commit()
    return _outs(db, clinic, [found], current.staff)[0]


@router.delete("/files/{file_id}", response_model=PatientFileOut)
def move_to_bin(file_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    found = _file(db, file_id)
    if found.deleted_at is None:
        found.deleted_at = now()
        found.deleted_by = current.staff.id
        found.purge_after = found.deleted_at + timedelta(days=BIN_DAYS)
        record(
            db, clinic_id=clinic.id, action="delete", actor_id=current.staff.id, entity_type="file", entity_id=found.id,
            patient_id=found.patient_id, changes={"moved_to_bin": True, "kind": found.kind, "title": found.title},
            request=request,
        )
        db.commit()
    return _outs(db, clinic, [found], current.staff)[0]


@router.post("/files/{file_id}/restore", response_model=PatientFileOut)
def restore_file(file_id: uuid.UUID, request: Request, current: Current = Depends(get_current)):
    db, clinic = current.db, current.clinic
    found = _file(db, file_id)
    if found.deleted_at is not None:
        if found.purge_after is not None and found.purge_after <= now():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This file has been removed for good.")
        found.deleted_at = found.deleted_by = found.purge_after = None
        record(
            db, clinic_id=clinic.id, action="restore", actor_id=current.staff.id, entity_type="file", entity_id=found.id,
            patient_id=found.patient_id, changes={"kind": found.kind, "title": found.title}, request=request,
        )
        db.commit()
    return _outs(db, clinic, [found], current.staff)[0]


def _download_name(clinic: Clinic, patient: Patient, found: File) -> str:
    day = found.uploaded_at.astimezone(ZoneInfo(clinic.timezone)).date()
    extension = found.storage_key.rsplit(".", 1)[-1]
    title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", found.title or KIND_LABEL[found.kind]).strip()
    return f"{patient_code(clinic, patient.patient_number)} {title} {date_text(day)}.{extension}"


@router.get("/files/content/{token}")
def file_content(token: str, request: Request, db: Session = Depends(get_db)):
    """Serves a file through its signed link, so it opens in an <img> or a new tab."""
    grant = read_view_token(token)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This link has expired. Open the file again from the patient's page.")
    use_clinic(db, grant.clinic_id)
    found = db.get(File, grant.file_id)
    if found is None or found.patient_id is None or found.kind not in PATIENT_FILE_KINDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this file.")

    storage = get_storage()
    if grant.thumbnail:
        if found.thumbnail_key is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This file has no preview.")
        return Response(
            content=storage.get(found.thumbnail_key), media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=1800"},
        )

    headers = {"Cache-Control": "private, max-age=1800"}
    if grant.download:
        clinic = db.get(Clinic, grant.clinic_id)
        name = _download_name(clinic, db.get(Patient, found.patient_id), found)
        plain = name.encode("ascii", "ignore").decode() or "file"
        headers["Content-Disposition"] = f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"
        headers["Cache-Control"] = "no-store"
        if grant.staff_id is not None:
            record(
                db, clinic_id=grant.clinic_id, action="download", actor_id=grant.staff_id, entity_type="file",
                entity_id=found.id, patient_id=found.patient_id, changes={"kind": found.kind, "title": found.title},
                request=request,
            )
            db.commit()
    return Response(content=storage.get(found.storage_key), media_type=found.content_type, headers=headers)
