"""Where file bytes live. Every key sits under the clinic's own folder: clinics/<clinic_id>/...

Local development and tests keep files on disk (backend/.storage). The live system will use
Amazon S3. Either way the browser uploads in the same steps (app/routers/patient_files.py):
ask for an upload link, send the bytes there, then confirm. Locally the link points at this
API; with S3 it will be a signed S3 link, and nothing else changes.
"""
import uuid
from pathlib import Path

from app.config import settings


def new_key(clinic_id: uuid.UUID, kind: str, extension: str) -> str:
    return f"clinics/{clinic_id}/{kind}/{uuid.uuid4().hex}.{extension}"


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("Storage key points outside the storage folder.")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def size(self, key: str) -> int | None:
        """Bytes stored under the key, or None if nothing arrived there."""
        path = self._path(key)
        return path.stat().st_size if path.is_file() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def get_storage() -> LocalStorage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_dir)
    raise NotImplementedError(f"Storage backend {settings.storage_backend!r} is not set up yet")
