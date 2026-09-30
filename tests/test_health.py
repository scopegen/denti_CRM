from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app


def test_health_reports_database_ok():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_api_connects_as_a_role_that_cannot_bypass_row_level_security():
    with engine.connect() as connection:
        row = connection.execute(
            text("select rolname, rolsuper, rolbypassrls from pg_roles where rolname = current_user")
        ).one()
    assert row.rolname == "denti_app"
    assert row.rolsuper is False
    assert row.rolbypassrls is False
