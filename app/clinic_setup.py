"""Everything a new clinic starts with: its owner and staff, the starter lists and services, and
its subscription on the Starter pack. Used by the demo seed, the create_clinic script and clinics
signing up by themselves."""
import uuid

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from app.clinic_time import add_months
from app.defaults import DEFAULT_PICKLISTS, STARTER_SERVICES, TOOTH_REMOVING_SERVICES
from app.models import Clinic, Picklist, Service, Staff, Subscription
from app.plans import STARTER_PACK_MONTHS
from app.security import now


def add_starter_data(db: Session, clinic_id: uuid.UUID, staff: list[dict], password_hash: str) -> list[Staff]:
    """The clinic's people, lists and services. The clinic must already be set for the
    transaction (row level security)."""
    people = [Staff(clinic_id=clinic_id, password_hash=password_hash, **person) for person in staff]
    db.add_all(people)
    for list_name, values in DEFAULT_PICKLISTS.items():
        for order, value in enumerate(values):
            db.add(Picklist(clinic_id=clinic_id, list=list_name, value=value, sort_order=order))
    for name, category, kind, rupees, recall in STARTER_SERVICES:
        db.add(
            Service(
                clinic_id=clinic_id, name=name, category=category, kind=kind,
                listed_price_paise=rupees * 100, recall_after_months=recall,
                removes_tooth=name in TOOTH_REMOVING_SERVICES,
            )
        )
    db.flush()
    return people


def set_up_clinic(connection: Connection, db: Session, clinic: Clinic, staff: list[dict], password_hash: str) -> None:
    """Adds the clinic and what it starts with. Runs as the migration role, in one transaction."""
    db.add(clinic)
    db.flush()
    # Staff and the rest are protected by row level security, so the clinic is set first.
    connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic.id)})
    # A new clinic is on the Starter pack: its first 3 months paid in one go.
    started = now()
    db.add(
        Subscription(
            clinic_id=clinic.id, billing_cycle="starter", status="active", starter_pack_used=True,
            current_period_start=started, current_period_end=add_months(started, STARTER_PACK_MONTHS),
        )
    )
    add_starter_data(db, clinic.id, staff, password_hash)
