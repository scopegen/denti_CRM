"""Creates a demo clinic to sign in to during local development.

    .\\.venv\\Scripts\\python -m app.seed

Runs as denti_owner (the migration role). Safe to run again: an existing demo clinic is left alone.
"""
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.clinic_time import add_months
from app.config import settings
from app.defaults import DEFAULT_PICKLISTS, STARTER_SERVICES, TOOTH_REMOVING_SERVICES
from app.models import Clinic, Picklist, Service, Staff, Subscription
from app.plans import STARTER_PACK_MONTHS
from app.security import hash_password, now

DEMO_PASSWORD = "DentiDemo123"

DEMO_CLINIC = {"slug": "smile-dental", "name": "Smile Dental Clinic", "plan": "standard", "patient_id_prefix": "SMILE"}

DEMO_STAFF = [
    {"name": "Dr Anika Sharma", "email": "owner@smiledental.test", "role": "owner", "treats_patients": True,
     "qualification": "BDS, MDS", "specialty": "Endodontist", "registration_no": "A-10234"},
    {"name": "Priya Verma", "email": "reception@smiledental.test", "role": "admin", "treats_patients": False},
    {"name": "Dr Rohan Mehta", "email": "doctor@smiledental.test", "role": "doctor", "treats_patients": True,
     "qualification": "BDS", "specialty": "General dentist", "registration_no": "A-20871"},
]


def main() -> None:
    engine = create_engine(settings.migration_database_url)
    with engine.begin() as connection:
        db = Session(bind=connection)
        clinic = db.scalar(select(Clinic).where(Clinic.slug == DEMO_CLINIC["slug"]))
        if clinic is not None:
            print(f"The demo clinic already exists ({clinic.name}). Nothing changed.")
            return

        clinic = Clinic(**DEMO_CLINIC)
        db.add(clinic)
        db.flush()
        # Staff rows are protected by row level security, so the clinic is set first.
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic.id)})
        password_hash = hash_password(DEMO_PASSWORD)
        for person in DEMO_STAFF:
            db.add(Staff(clinic_id=clinic.id, password_hash=password_hash, **person))
        for list_name, values in DEFAULT_PICKLISTS.items():
            for order, value in enumerate(values):
                db.add(Picklist(clinic_id=clinic.id, list=list_name, value=value, sort_order=order))
        # Like a new clinic: on the Starter pack, its first 3 months paid in one go.
        started = now()
        db.add(
            Subscription(
                clinic_id=clinic.id, billing_cycle="starter", status="active", starter_pack_used=True,
                current_period_start=started, current_period_end=add_months(started, STARTER_PACK_MONTHS),
            )
        )
        for name, category, kind, rupees, recall in STARTER_SERVICES:
            db.add(
                Service(
                    clinic_id=clinic.id, name=name, category=category, kind=kind,
                    listed_price_paise=rupees * 100, recall_after_months=recall,
                    removes_tooth=name in TOOTH_REMOVING_SERVICES,
                )
            )
        db.flush()

    print(f"Created {DEMO_CLINIC['name']}: http://localhost:5180/{DEMO_CLINIC['slug']}  Password for every login: {DEMO_PASSWORD}")
    for person in DEMO_STAFF:
        print(f"  {person['role']:<7} {person['email']}")


if __name__ == "__main__":
    main()
