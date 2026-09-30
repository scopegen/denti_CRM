from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile"):
    clinic_id = make_clinic(slug)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, doctor


def _patient(client, token):
    response = client.post("/patients", json={"name": "Rahul Sharma", "phone": "98100 12345", "birth_year": 1990}, headers=auth(token))
    return response.json()


def _service(client, token, name="Root canal treatment"):
    body = {"name": name, "kind": "in_house", "listed_price_paise": 650000}
    return client.post("/services", json=body, headers=auth(token)).json()


def _consultation(doctor_id, **extra):
    body = {
        "doctor_id": str(doctor_id),
        "chief_complaint": "Pain in lower left molar for 3 days",
        "oral_examination": "Deep caries on 36",
        "xray_taken": True,
        "prescription": {
            "diagnosis": "Irreversible pulpitis, 36",
            "advice": "Warm saline rinses",
            "items": [{"medicine": "Amoxicillin 500 mg", "frequency": "TDS", "duration": "5 days"}],
        },
    }
    body.update(extra)
    return body


def _ledger(clinic_id):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(text("SELECT kind, amount_paise, effective_at FROM ledger_entries")).all()


def test_consultation_saves_with_prescription_and_adds_the_fee_to_the_bill(client):
    clinic_id, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    service = _service(client, token)

    created = client.post(
        f"/patients/{patient['id']}/consultations",
        json=_consultation(doctor.id, recommendations=[{"service_id": service["id"], "teeth": [36]}]),
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["doctor"]["name"] == "Doc"
    assert body["recommendations"] == [{"service_id": service["id"], "service_name": "Root canal treatment", "teeth": [36]}]
    assert body["prescription"]["version"] == 1
    assert body["prescription"]["items"][0]["medicine"] == "Amoxicillin 500 mg"
    # The fee never reaches the consultation shown to doctors.
    assert "fee" not in body

    assert [(k, a) for k, a, _ in _ledger(clinic_id)] == [("consultation_fee", 50000)]


def test_saving_a_consultation_marks_the_patient_seen(client):
    _, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))
    client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(token))
    arrived = client.get("/today", headers=auth(token)).json()["arrived"]
    assert arrived[0]["seen_at"] is not None


def test_only_the_prescribing_doctor_can_change_the_prescription(client):
    _, owner, doctor = _setup()
    owner_token = sign_in(client, owner)
    patient = _patient(client, owner_token)
    created = client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(owner_token)).json()
    assert created["prescription"]["can_edit"] is False  # the owner did not prescribe it

    changed = _consultation(doctor.id, chief_complaint="Changed by the owner")
    changed["prescription"]["advice"] = "Different advice"
    refused = client.patch(f"/consultations/{created['id']}", json=changed, headers=auth(owner_token))
    assert refused.status_code == 403
    # Nothing in the request was saved, not even the complaint.
    listed = client.get(f"/patients/{patient['id']}/consultations", headers=auth(owner_token)).json()
    assert listed[0]["chief_complaint"] == "Pain in lower left molar for 3 days"

    doctor_token = sign_in(client, doctor)
    allowed = client.patch(f"/consultations/{created['id']}", json=changed, headers=auth(doctor_token))
    assert allowed.status_code == 200
    assert allowed.json()["prescription"]["version"] == 2
    assert allowed.json()["prescription"]["can_edit"] is True


def test_every_prescription_version_is_kept_including_diagnosis_and_advice(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    created = client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(token)).json()

    edit = _consultation(doctor.id)
    edit["prescription"]["diagnosis"] = "Reversible pulpitis, 36"
    client.patch(f"/consultations/{created['id']}", json=edit, headers=auth(token))

    versions = client.get(f"/prescriptions/{created['prescription']['id']}/versions", headers=auth(token)).json()
    assert [(v["version"], v["diagnosis"]) for v in versions] == [(2, "Reversible pulpitis, 36"), (1, "Irreversible pulpitis, 36")]


def test_editing_notes_alone_does_not_create_a_prescription_version(client):
    _, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    created = client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(token)).json()

    updated = client.patch(
        f"/consultations/{created['id']}", json=_consultation(doctor.id, oral_examination="Deep caries on 36 and 37"), headers=auth(token)
    )
    assert updated.status_code == 200
    assert updated.json()["prescription"]["version"] == 1

    log = client.get("/audit-log", headers=auth(token)).json()["items"]
    change = next(e for e in log if e["action"] == "update" and e["entity_type"] == "consultation")
    assert change["changes"] == {"oral_examination": ["Deep caries on 36", "Deep caries on 36 and 37"]}


def test_dates_can_be_earlier_but_not_in_the_future(client):
    clinic_id, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    last_week = (date.today() - timedelta(days=7)).isoformat()

    future = client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id, consult_date=tomorrow), headers=auth(token))
    assert future.status_code == 422
    past = client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id, consult_date=last_week), headers=auth(token))
    assert past.status_code == 201
    [(_, _, effective_at)] = _ledger(clinic_id)
    assert effective_at.date().isoformat() == last_week


def test_tooth_numbers_are_checked(client):
    _, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    service = _service(client, token)
    bad = client.post(
        f"/patients/{patient['id']}/consultations",
        json=_consultation(doctor.id, recommendations=[{"service_id": service["id"], "teeth": [19]}]),
        headers=auth(token),
    )
    assert bad.status_code == 422


def test_services_from_another_clinic_are_refused(client):
    _, owner_a, doctor_a = _setup("alpha")
    _, owner_b, _ = _setup("beta")
    service_b = _service(client, sign_in(client, owner_b))
    token_a = sign_in(client, owner_a)
    patient = _patient(client, token_a)
    response = client.post(
        f"/patients/{patient['id']}/consultations",
        json=_consultation(doctor_a.id, recommendations=[{"service_id": service_b["id"], "teeth": []}]),
        headers=auth(token_a),
    )
    assert response.status_code == 400


def test_no_fee_entry_when_the_clinic_charges_nothing(client):
    clinic_id, owner, doctor = _setup()
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(
            text("INSERT INTO clinic_settings (clinic_id, default_consultation_fee_paise) VALUES (:cid, 0)"), {"cid": clinic_id}
        )
    token = sign_in(client, owner)
    patient = _patient(client, token)
    client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(token))
    assert _ledger(clinic_id) == []


def test_the_bill_cannot_be_edited_or_deleted_by_the_api(client):
    clinic_id, owner, doctor = _setup()
    token = sign_in(client, owner)
    patient = _patient(client, token)
    client.post(f"/patients/{patient['id']}/consultations", json=_consultation(doctor.id), headers=auth(token))
    for statement in ("UPDATE ledger_entries SET amount_paise = 1", "DELETE FROM ledger_entries"):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
                connection.execute(text(statement))
