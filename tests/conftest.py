import os
import re
import tempfile
import uuid
from dataclasses import dataclass

# Tests always run against denti_test, never the development database. These are set
# before the app is imported, so they win over backend/.env.
os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://denti_app:denti_app_dev@localhost:5434/denti_test")
os.environ.setdefault(
    "TEST_MIGRATION_DATABASE_URL", "postgresql+psycopg://denti_owner:denti_owner_dev@localhost:5434/denti_test"
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["MIGRATION_DATABASE_URL"] = os.environ["TEST_MIGRATION_DATABASE_URL"]
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"
os.environ["EMAIL_BACKEND"] = "console"
os.environ["BCRYPT_ROUNDS"] = "4"
# The test client calls the API directly, without the /api prefix the browser uses.
os.environ["API_PATH_PREFIX"] = ""
# Stored files (signatures, X-rays) go to a throwaway folder, never backend/.storage.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="denti-test-storage-")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import email  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "CorrectHorse1"
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
owner_engine = create_engine(os.environ["TEST_MIGRATION_DATABASE_URL"])


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Rebuilds the test database from the migrations once per test run, which also
    checks that the migrations run down and up cleanly."""
    config = Config(os.path.join(_BACKEND_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(_BACKEND_DIR, "alembic"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE audit_log, plan_changes, message_log, share_links, message_templates, invoice_lines, invoices, estimate_items, estimate_options, estimates, ledger_entries, tooth_findings, mouth_checks, prescription_versions, prescriptions, "
                "treatment_handoffs, visits, "
                "treatments, consultation_recommendations, consultations, clinic_settings, arrivals, follow_ups, services, "
                "patients, import_batches, picklists, counters, trusted_devices, login_codes, auth_sessions, "
                "files, subscriptions, staff, clinics RESTART IDENTITY CASCADE"
            )
        )
    email.outbox.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@dataclass
class Person:
    id: uuid.UUID
    clinic_id: uuid.UUID
    clinic_slug: str
    email: str
    role: str


def make_clinic(slug: str, plan: str = "standard") -> uuid.UUID:
    clinic_id = uuid.uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO clinics (id, slug, name, plan) VALUES (:id, :slug, :name, :plan)"),
            {"id": clinic_id, "slug": slug, "name": f"{slug.title()} Dental", "plan": plan},
        )
    return clinic_id


def make_staff(clinic_id: uuid.UUID, slug: str, email_address: str, role: str, status: str = "active") -> Person:
    staff_id = uuid.uuid4()
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(
            text(
                "INSERT INTO staff (id, clinic_id, name, email, password_hash, role, treats_patients, status) "
                "VALUES (:id, :clinic_id, :name, :email, :password_hash, :role, :treats, :status)"
            ),
            {
                "id": staff_id,
                "clinic_id": clinic_id,
                "name": email_address.split("@")[0].title(),
                "email": email_address,
                "password_hash": hash_password(PASSWORD),
                "role": role,
                "treats": role != "admin",
                "status": status,
            },
        )
    return Person(id=staff_id, clinic_id=clinic_id, clinic_slug=slug, email=email_address, role=role)


def add_picklist(clinic_id: uuid.UUID, list_name: str, values: list[str]) -> None:
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        for order, value in enumerate(values):
            connection.execute(
                text(
                    "INSERT INTO picklists (id, clinic_id, list, value, sort_order) "
                    "VALUES (gen_random_uuid(), :cid, :list, :value, :order)"
                ),
                {"cid": clinic_id, "list": list_name, "value": value, "order": order},
            )


def last_code_for(address: str) -> str:
    for message in reversed(email.outbox):
        if message.to == address:
            match = re.search(r"\b(\d{6})\b", message.subject)
            assert match, message.subject
            return match.group(1)
    raise AssertionError(f"No email was sent to {address}")


def sign_in(client: TestClient, person: Person, trust_device: bool = False) -> str:
    """Signs in with password and emailed code. Returns the access token."""
    response = client.post("/auth/login", json={"clinic": person.clinic_slug, "email": person.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    if body["status"] == "signed_in":
        return body["session"]["access_token"]
    response = client.post(
        "/auth/verify",
        json={
            "clinic": person.clinic_slug,
            "challenge_id": body["challenge_id"],
            "code": last_code_for(person.email),
            "trust_device": trust_device,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
