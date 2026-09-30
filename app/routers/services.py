import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit import record
from app.deps import Current, get_current, require_admin
from app.models import Service
from app.schemas import ServiceIn, ServiceOut
from app.security import now

# The clinic's service catalog. Everyone reads it (doctors pick services); only the owner and
# receptionist change it, because it holds prices. Services are retired, never deleted.

router = APIRouter(prefix="/services", tags=["services"])

_FIELDS = ["name", "category", "kind", "listed_price_paise", "recall_after_months", "removes_tooth", "active"]


@router.get("", response_model=list[ServiceOut])
def list_services(include_retired: bool = False, current: Current = Depends(get_current)):
    """Doctors get the list without prices."""
    stmt = select(Service).order_by(func.lower(func.coalesce(Service.category, "")), func.lower(Service.name))
    if not include_retired:
        stmt = stmt.where(Service.active.is_(True))
    services = [ServiceOut.model_validate(s) for s in current.db.scalars(stmt)]
    if current.staff.role not in ("owner", "admin"):
        for service in services:
            service.listed_price_paise = None
    return services


def _name_taken(current: Current, name: str, except_id: uuid.UUID | None = None) -> bool:
    stmt = select(Service.id).where(func.lower(Service.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(Service.id != except_id)
    return current.db.scalar(stmt) is not None


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
def create_service(payload: ServiceIn, request: Request, current: Current = Depends(require_admin)):
    if _name_taken(current, payload.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A service with this name already exists.")
    service = Service(clinic_id=current.clinic.id, **payload.model_dump())
    current.db.add(service)
    current.db.flush()
    record(
        current.db, clinic_id=current.clinic.id, action="create", actor_id=current.staff.id, entity_type="service",
        entity_id=service.id, changes={f: getattr(service, f) for f in _FIELDS}, request=request,
    )
    try:
        current.db.commit()
    except IntegrityError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A service with this name already exists.")
    return service


@router.patch("/{service_id}", response_model=ServiceOut)
def update_service(service_id: uuid.UUID, payload: ServiceIn, request: Request, current: Current = Depends(require_admin)):
    """Changing a price here never changes treatments already added: they keep the price
    they were added at."""
    db = current.db
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We could not find this service.")
    if _name_taken(current, payload.name, except_id=service.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A service with this name already exists.")

    changes = {}
    for field, value in payload.model_dump().items():
        if getattr(service, field) != value:
            changes[field] = [getattr(service, field), value]
            setattr(service, field, value)
    if changes:
        service.updated_at = now()
        record(
            db, clinic_id=current.clinic.id, action="update", actor_id=current.staff.id, entity_type="service",
            entity_id=service.id, changes=changes, request=request,
        )
        db.commit()
    return service
