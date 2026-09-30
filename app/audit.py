import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditLog


def request_ip(request: Request | None) -> str | None:
    return request.client.host if request and request.client else None


def request_device(request: Request | None) -> str | None:
    if request is None:
        return None
    agent = request.headers.get("user-agent")
    return agent[:200] if agent else None


def record(
    db: Session,
    *,
    clinic_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "staff",
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Add one audit log row. It is saved with the caller's commit, so the log entry and
    the change it describes are saved together or not at all."""
    db.add(
        AuditLog(
            clinic_id=clinic_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            patient_id=patient_id,
            changes=changes,
            ip=request_ip(request),
            device=request_device(request),
        )
    )
