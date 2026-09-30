from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Clinic, ClinicSettings


def get_settings(db: Session, clinic: Clinic) -> ClinicSettings:
    """The clinic's settings row, created with defaults the first time it is needed."""
    db.execute(insert(ClinicSettings).values(clinic_id=clinic.id).on_conflict_do_nothing(index_elements=["clinic_id"]))
    return db.get(ClinicSettings, clinic.id)
