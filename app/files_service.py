"""Stored files: saving, reading, the signed links used to upload and view them, and thumbnails.

A signed link carries its own permission, the way an S3 signed link does: whoever has it can
do exactly one thing (send one file for one patient, or read one file) until it expires. The
API hands them out only to people signed in to the clinic.
"""
import io
import math
import time
import uuid
from dataclasses import dataclass

import jwt
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import File
from app.storage import get_storage, new_key

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
_OTHER_EXTENSIONS = {
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

# What can be uploaded to a patient's record. X-rays arrive as JPG or PNG exports from the
# sensor software or a phone photo of the film; reports and referral letters as PDF.
PATIENT_FILE_TYPES = frozenset(_EXTENSIONS)
PATIENT_FILE_KINDS = ("xray", "photo", "document")
MAX_PATIENT_FILE_BYTES = 25 * 1024 * 1024

UPLOAD_LINK_SECONDS = 15 * 60
# Viewing links are the same for everyone within a half hour window, so the browser can
# reuse images it already has instead of downloading them again.
VIEW_LINK_WINDOW_SECONDS = 30 * 60

THUMBNAIL_PIXELS = 480

_ALGORITHM = "HS256"
_UPLOAD_AUDIENCE = "denti-upload"
_VIEW_AUDIENCE = "denti-file"


def store_file(
    db: Session,
    *,
    clinic_id: uuid.UUID,
    kind: str,
    data: bytes,
    content_type: str,
    uploaded_by: uuid.UUID,
    width: int | None = None,
    height: int | None = None,
    patient_id: uuid.UUID | None = None,
) -> File:
    """Saves the bytes in the clinic's folder and records the file. Joins the caller's transaction."""
    key = new_key(clinic_id, kind, _EXTENSIONS.get(content_type) or _OTHER_EXTENSIONS.get(content_type, "bin"))
    get_storage().put(key, data, content_type)
    record = File(
        clinic_id=clinic_id, patient_id=patient_id, kind=kind, storage_key=key, content_type=content_type,
        size_bytes=len(data), width=width, height=height, uploaded_by=uploaded_by,
    )
    db.add(record)
    db.flush()
    return record


def read_file(record: File) -> bytes:
    return get_storage().get(record.storage_key)


def storage_used(db: Session) -> int:
    """Bytes the clinic's files take. Files in the bin count until they are removed for good."""
    return db.scalar(select(func.coalesce(func.sum(File.size_bytes), 0)))


def new_patient_file_key(clinic_id: uuid.UUID, content_type: str) -> str:
    return new_key(clinic_id, "patients", _EXTENSIONS[content_type])


# Upload links

@dataclass(frozen=True)
class UploadGrant:
    clinic_id: uuid.UUID
    patient_id: uuid.UUID
    storage_key: str
    content_type: str
    max_bytes: int


def upload_token(grant: UploadGrant) -> str:
    payload = {
        "aud": _UPLOAD_AUDIENCE,
        "cid": str(grant.clinic_id),
        "pid": str(grant.patient_id),
        "key": grant.storage_key,
        "ct": grant.content_type,
        "max": grant.max_bytes,
        "exp": int(time.time()) + UPLOAD_LINK_SECONDS,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def read_upload_token(token: str) -> UploadGrant | None:
    try:
        p = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM], audience=_UPLOAD_AUDIENCE)
        return UploadGrant(
            clinic_id=uuid.UUID(p["cid"]), patient_id=uuid.UUID(p["pid"]), storage_key=str(p["key"]),
            content_type=str(p["ct"]), max_bytes=int(p["max"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def upload_url(token: str) -> str:
    if settings.storage_backend == "local":
        return f"{settings.api_path_prefix}/uploads/{token}"
    raise NotImplementedError("Signed S3 upload links come with the S3 storage backend")


# Viewing links

@dataclass(frozen=True)
class ViewGrant:
    clinic_id: uuid.UUID
    file_id: uuid.UUID
    thumbnail: bool
    download: bool
    # Download links name who asked for them, for the activity log.
    staff_id: uuid.UUID | None = None


def view_token(grant: ViewGrant, now: float | None = None) -> str:
    moment = time.time() if now is None else now
    window_end = math.ceil(moment / VIEW_LINK_WINDOW_SECONDS) * VIEW_LINK_WINDOW_SECONDS
    payload = {
        "aud": _VIEW_AUDIENCE,
        "cid": str(grant.clinic_id),
        "fid": str(grant.file_id),
        "t": grant.thumbnail,
        "d": grant.download,
        # Valid for at least one full window from now.
        "exp": window_end + VIEW_LINK_WINDOW_SECONDS,
    }
    if grant.staff_id is not None:
        payload["sid"] = str(grant.staff_id)
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def read_view_token(token: str) -> ViewGrant | None:
    try:
        p = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM], audience=_VIEW_AUDIENCE)
        return ViewGrant(
            clinic_id=uuid.UUID(p["cid"]), file_id=uuid.UUID(p["fid"]), thumbnail=bool(p["t"]), download=bool(p["d"]),
            staff_id=uuid.UUID(p["sid"]) if "sid" in p else None,
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def view_url(grant: ViewGrant) -> str:
    if settings.storage_backend == "local":
        return f"{settings.api_path_prefix}/files/content/{view_token(grant)}"
    raise NotImplementedError("Signed S3 viewing links come with the S3 storage backend")


# Thumbnails

class NotAnImage(ValueError):
    pass


def make_thumbnail(data: bytes) -> tuple[bytes, int, int]:
    """Returns (JPEG thumbnail, original width, original height). Phone photos are turned the
    right way up. Raises NotAnImage if the bytes are not a readable image."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise NotAnImage("That file is not a readable image.")
    image = ImageOps.exif_transpose(image)
    width, height = image.size
    if image.mode not in ("RGB", "L"):
        # Transparent PNGs get a white background rather than black.
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.convert("RGBA").getchannel("A"))
        image = background
    image.thumbnail((THUMBNAIL_PIXELS, THUMBNAIL_PIXELS), Image.LANCZOS)
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue(), width, height
