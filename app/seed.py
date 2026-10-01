"""Creates a demo clinic to sign in to during local development.

    .\\.venv\\Scripts\\python -m app.seed

Runs as denti_owner (the migration role). Safe to run again: an existing demo clinic is left alone.
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clinic_setup import set_up_clinic
from app.config import settings
from app.models import Clinic
from app.security import hash_password

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

        set_up_clinic(connection, db, Clinic(**DEMO_CLINIC), DEMO_STAFF, hash_password(DEMO_PASSWORD))

    print(f"Created {DEMO_CLINIC['name']}: http://localhost:5180/{DEMO_CLINIC['slug']}  Password for every login: {DEMO_PASSWORD}")
    for person in DEMO_STAFF:
        print(f"  {person['role']:<7} {person['email']}")


if __name__ == "__main__":
    main()
