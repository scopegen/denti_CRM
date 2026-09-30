from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db, use_clinic
from app.models import Clinic, Staff
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Current:
    """The signed in person and their clinic. The session is already scoped to the clinic."""

    staff: Staff
    clinic: Clinic
    db: Session


def get_current(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Current:
    signed_out = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in again.")
    if credentials is None:
        raise signed_out
    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise signed_out

    use_clinic(db, claims.clinic_id)
    clinic = db.get(Clinic, claims.clinic_id)
    staff = db.get(Staff, claims.staff_id)
    if (
        clinic is None
        or staff is None
        or staff.status != "active"
        or staff.token_version != claims.token_version
        or clinic.status in ("frozen", "closed")
    ):
        raise signed_out
    return Current(staff=staff, clinic=clinic, db=db)


def require_roles(*roles: str) -> Callable[[Current], Current]:
    """Every endpoint states who may use it. Doctors never reach money or admin screens."""

    def check(current: Current = Depends(get_current)) -> Current:
        if current.staff.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this.")
        return current

    return check


require_admin = require_roles("owner", "admin")
require_owner = require_roles("owner")
