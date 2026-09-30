"""The most important tests: one clinic can never see or change another clinic's data."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from tests.conftest import auth, make_clinic, make_staff, sign_in


def test_each_clinic_sees_only_its_own_staff(client):
    a = make_clinic("alpha")
    b = make_clinic("beta")
    owner_a = make_staff(a, "alpha", "owner@alpha.test", "owner")
    make_staff(a, "alpha", "doc@alpha.test", "doctor")
    owner_b = make_staff(b, "beta", "owner@beta.test", "owner")
    make_staff(b, "beta", "doc@beta.test", "doctor")

    staff_a = client.get("/staff", headers=auth(sign_in(client, owner_a))).json()
    staff_b = client.get("/staff", headers=auth(sign_in(client, owner_b))).json()
    assert {s["email"] for s in staff_a} == {"owner@alpha.test", "doc@alpha.test"}
    assert {s["email"] for s in staff_b} == {"owner@beta.test", "doc@beta.test"}


def test_another_clinics_record_looks_like_it_does_not_exist(client):
    a = make_clinic("alpha")
    b = make_clinic("beta")
    owner_a = make_staff(a, "alpha", "owner@alpha.test", "owner")
    doctor_b = make_staff(b, "beta", "doc@beta.test", "doctor")

    response = client.post(f"/staff/{doctor_b.id}/deactivate", headers=auth(sign_in(client, owner_a)))
    assert response.status_code == 404


def test_same_email_at_two_clinics_signs_in_to_the_right_one(client):
    a = make_clinic("alpha")
    b = make_clinic("beta")
    at_a = make_staff(a, "alpha", "dr.rao@mail.test", "owner")
    at_b = make_staff(b, "beta", "dr.rao@mail.test", "owner")

    me_a = client.get("/auth/me", headers=auth(sign_in(client, at_a))).json()
    me_b = client.get("/auth/me", headers=auth(sign_in(client, at_b))).json()
    assert me_a["clinic"]["slug"] == "alpha"
    assert me_b["clinic"]["slug"] == "beta"
    assert me_a["staff"]["id"] != me_b["staff"]["id"]


def test_one_browser_stays_signed_in_to_two_clinics_separately(client):
    a = make_clinic("alpha")
    b = make_clinic("beta")
    sign_in(client, make_staff(a, "alpha", "owner@alpha.test", "owner"))
    sign_in(client, make_staff(b, "beta", "owner@beta.test", "owner"))

    at_a = client.post("/auth/refresh", json={"clinic": "alpha"})
    at_b = client.post("/auth/refresh", json={"clinic": "beta"})
    assert at_a.json()["clinic"]["slug"] == "alpha"
    assert at_b.json()["clinic"]["slug"] == "beta"

    # Signing out of one clinic leaves the other signed in.
    client.post("/auth/logout", json={"clinic": "alpha"})
    assert client.post("/auth/refresh", json={"clinic": "alpha"}).status_code == 401
    assert client.post("/auth/refresh", json={"clinic": "beta"}).status_code == 200


def test_reserved_words_cannot_be_clinic_addresses():
    with pytest.raises(DBAPIError):
        make_clinic("api")


def test_database_hides_other_clinics_rows_even_if_the_code_forgets_to_filter():
    a = make_clinic("alpha")
    b = make_clinic("beta")
    make_staff(a, "alpha", "owner@alpha.test", "owner")
    make_staff(b, "beta", "owner@beta.test", "owner")

    with engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(a)})
        emails = connection.execute(text("SELECT email FROM staff")).scalars().all()
    assert emails == ["owner@alpha.test"]


def test_database_refuses_to_write_a_row_for_another_clinic():
    a = make_clinic("alpha")
    b = make_clinic("beta")
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(a)})
            connection.execute(
                text(
                    "INSERT INTO staff (id, clinic_id, name, email, password_hash, role) "
                    "VALUES (gen_random_uuid(), :other, 'Intruder', 'x@beta.test', 'x', 'doctor')"
                ),
                {"other": b},
            )


def test_no_clinic_set_means_no_data_at_all():
    a = make_clinic("alpha")
    make_staff(a, "alpha", "owner@alpha.test", "owner")
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("SELECT email FROM staff")).all()


def test_the_api_role_cannot_change_or_delete_the_audit_log(client):
    a = make_clinic("alpha")
    owner = make_staff(a, "alpha", "owner@alpha.test", "owner")
    sign_in(client, owner)
    for statement in ("UPDATE audit_log SET action = 'edited'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(a)})
                connection.execute(text(statement))


def test_the_api_role_cannot_create_or_change_clinics():
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO clinics (id, slug, name) VALUES (gen_random_uuid(), 'sneaky', 'Sneaky')"))
