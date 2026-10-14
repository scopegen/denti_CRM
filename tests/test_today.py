from datetime import date, timedelta

from sqlalchemy import text

from app.clinic_time import clinic_today
from app.db import SessionLocal, use_clinic
from app.models import Clinic
from app.routers.today import mark_seen
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile"):
    clinic_id = make_clinic(slug)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    return clinic_id, owner


def _register(client, token, name="Rahul Sharma", phone="10100 12345"):
    response = client.post("/patients", json={"name": name, "phone": phone, "birth_year": 1990}, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


def _today(client, token, **params):
    return client.get("/today", params=params, headers=auth(token)).json()


def test_walk_in_shows_as_waiting_and_cannot_be_added_twice(client):
    _, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)

    first = client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))
    assert first.status_code == 201
    again = client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))
    assert again.status_code == 409
    assert again.json()["detail"] == "Rahul Sharma is already waiting."

    arrived = _today(client, token)["arrived"]
    assert [a["patient"]["name"] for a in arrived] == ["Rahul Sharma"]
    assert arrived[0]["seen_at"] is None


def test_mistaken_arrival_is_cancelled_not_deleted(client):
    clinic_id, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    arrival = client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token)).json()

    assert client.post(f"/arrivals/{arrival['id']}/cancel", headers=auth(token)).status_code == 204
    assert _today(client, token)["arrived"] == []
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        assert connection.execute(text("SELECT count(*) FROM arrivals WHERE cancelled_at IS NOT NULL")).scalar_one() == 1


def test_expected_visit_disappears_once_the_patient_arrives(client):
    _, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    today = date.today().isoformat()
    client.post(
        f"/patients/{patient['id']}/follow-ups",
        json={"kind": "visit", "due_on": today, "reason": "Root canal sitting 2"},
        headers=auth(token),
    )

    expected = _today(client, token)["expected"]
    assert [(e["patient"]["name"], e["reason"]) for e in expected] == [("Rahul Sharma", "Root canal sitting 2")]

    client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))
    assert _today(client, token)["expected"] == []
    history = client.get(f"/patients/{patient['id']}/follow-ups", headers=auth(token)).json()
    assert history[0]["status"] == "done"


def test_calls_due_are_listed_until_marked_done(client):
    _, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    follow_up = client.post(
        f"/patients/{patient['id']}/follow-ups",
        json={"kind": "call", "due_on": date.today().isoformat(), "reason": "Check pain after extraction"},
        headers=auth(token),
    ).json()

    assert len(_today(client, token)["calls"]) == 1
    done = client.post(f"/follow-ups/{follow_up['id']}/done", headers=auth(token))
    assert done.json()["status"] == "done"
    assert _today(client, token)["calls"] == []


def test_overdue_follow_ups_stay_on_the_list_and_are_flagged(client):
    clinic_id, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    follow_up = client.post(
        f"/patients/{patient['id']}/follow-ups",
        json={"kind": "call", "due_on": date.today().isoformat()},
        headers=auth(token),
    ).json()
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(text("UPDATE follow_ups SET due_on = due_on - 3 WHERE id = :id"), {"id": follow_up["id"]})

    calls = _today(client, token)["calls"]
    assert len(calls) == 1 and calls[0]["overdue"] is True


def test_follow_up_cannot_be_set_in_the_past(client):
    _, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    response = client.post(
        f"/patients/{patient['id']}/follow-ups", json={"kind": "call", "due_on": yesterday}, headers=auth(token)
    )
    assert response.status_code == 422


def test_saving_a_consultation_will_mark_the_arrival_seen(client):
    clinic_id, owner = _setup()
    token = sign_in(client, owner)
    patient = _register(client, token)
    client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))

    db = SessionLocal()
    try:
        use_clinic(db, clinic_id)
        clinic = db.get(Clinic, clinic_id)
        assert clinic_today(clinic) is not None
        mark_seen(db, clinic, patient["id"])
        db.commit()
    finally:
        db.close()

    assert _today(client, token)["arrived"][0]["seen_at"] is not None


def test_filter_arrivals_by_doctor(client):
    clinic_id, owner = _setup()
    doctor = make_staff(clinic_id, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, owner)
    rahul = _register(client, token)
    neha = _register(client, token, "Neha Singh", "10990 55555")
    client.post("/arrivals", json={"patient_id": rahul["id"], "preferred_doctor_id": str(doctor.id)}, headers=auth(token))
    client.post("/arrivals", json={"patient_id": neha["id"]}, headers=auth(token))

    mine = _today(client, token, doctor_id=str(doctor.id))["arrived"]
    assert [a["patient"]["name"] for a in mine] == ["Rahul Sharma"]
    assert mine[0]["preferred_doctor"]["name"] == "Doc"


def test_doctors_list_is_readable_by_everyone(client):
    clinic_id, owner = _setup()
    make_staff(clinic_id, "smile", "doc@smile.test", "doctor")
    front = make_staff(clinic_id, "smile", "front@smile.test", "admin")
    names = [d["name"] for d in client.get("/staff/doctors", headers=auth(sign_in(client, front))).json()]
    assert names == ["Doc", "Owner"]


def test_another_clinics_patient_cannot_be_marked_arrived(client):
    _, owner_a = _setup("alpha")
    _, owner_b = _setup("beta")
    token_b = sign_in(client, owner_b)
    patient_b = _register(client, token_b)
    token_a = sign_in(client, owner_a)
    response = client.post("/arrivals", json={"patient_id": patient_b["id"]}, headers=auth(token_a))
    assert response.status_code == 404
