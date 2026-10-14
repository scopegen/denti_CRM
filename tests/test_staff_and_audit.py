from tests.conftest import PASSWORD, auth, make_clinic, make_staff, sign_in


def _new_staff(role: str, email_address: str) -> dict:
    return {"name": "New Person", "email": email_address, "role": role, "password": "StrongPass1"}


def test_owner_adds_staff_within_the_plan_limits(client):
    clinic = make_clinic("basicclinic", plan="basic")
    owner = make_staff(clinic, "basicclinic", "owner@basic.test", "owner")
    token = sign_in(client, owner)

    # Basic includes 1 receptionist and 1 doctor, besides the free owner login.
    assert client.post("/staff", json=_new_staff("admin", "front@basic.test"), headers=auth(token)).status_code == 201
    assert client.post("/staff", json=_new_staff("doctor", "doc@basic.test"), headers=auth(token)).status_code == 201

    over = client.post("/staff", json=_new_staff("doctor", "doc2@basic.test"), headers=auth(token))
    assert over.status_code == 409
    assert over.json()["detail"] == "Your Basic plan includes 1 doctor login. Upgrade your plan to add more."


def test_email_must_be_unique_within_the_clinic(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    client.post("/staff", json=_new_staff("doctor", "doc@smile.test"), headers=auth(token))
    again = client.post("/staff", json=_new_staff("doctor", "DOC@smile.test"), headers=auth(token))
    assert again.status_code == 409


def test_doctors_cannot_manage_staff(client):
    clinic = make_clinic("smile")
    make_staff(clinic, "smile", "owner@smile.test", "owner")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, doctor)
    assert client.get("/staff", headers=auth(token)).status_code == 403
    assert client.post("/staff", json=_new_staff("doctor", "x@smile.test"), headers=auth(token)).status_code == 403


def test_deactivating_signs_the_person_out_everywhere(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")

    from fastapi.testclient import TestClient
    from app.main import app

    doctor_client = TestClient(app)
    doctor_token = sign_in(doctor_client, doctor)
    assert doctor_client.get("/auth/me", headers=auth(doctor_token)).status_code == 200

    owner_token = sign_in(client, owner)
    assert client.post(f"/staff/{doctor.id}/deactivate", headers=auth(owner_token)).status_code == 200

    assert doctor_client.get("/auth/me", headers=auth(doctor_token)).status_code == 401
    assert doctor_client.post("/auth/refresh", json={"clinic": "smile"}).status_code == 401
    login = doctor_client.post("/auth/login", json={"clinic": "smile", "email": doctor.email, "password": PASSWORD})
    assert login.status_code == 401


def test_owner_login_cannot_be_deactivated(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    admin = make_staff(clinic, "smile", "front@smile.test", "admin")
    token = sign_in(client, admin)
    response = client.post(f"/staff/{owner.id}/deactivate", headers=auth(token))
    assert response.status_code == 400


def test_only_the_owner_can_read_the_audit_log(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    admin = make_staff(clinic, "smile", "front@smile.test", "admin")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")

    assert client.get("/audit-log", headers=auth(sign_in(client, admin))).status_code == 403
    assert client.get("/audit-log", headers=auth(sign_in(client, doctor))).status_code == 403

    entries = client.get("/audit-log", headers=auth(sign_in(client, owner))).json()["items"]
    actions = [entry["action"] for entry in entries]
    assert "sign_in" in actions
    assert "sign_in_code_sent" in actions


def test_failed_sign_ins_are_logged(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    client.post("/auth/login", json={"clinic": "smile", "email": owner.email, "password": "wrong"})
    entries = client.get("/audit-log", headers=auth(sign_in(client, owner))).json()["items"]
    failed = [entry for entry in entries if entry["action"] == "sign_in_failed"]
    assert len(failed) == 1
    assert failed[0]["changes"] == {"email": "owner@smile.test"}


def test_the_activity_log_filters_by_person_patient_action_and_date(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    desk = make_staff(clinic_id, "smile", "desk@smile.test", "admin")
    desk_token = sign_in(client, desk)
    first = client.post("/patients", json={"name": "Asha Rao", "phone": "1010000001", "birth_year": 1990}, headers=auth(desk_token)).json()
    client.post("/patients", json={"name": "Vikram Singh", "phone": "1010000002", "birth_year": 1980}, headers=auth(desk_token))
    client.get(f"/patients/{first['code']}", headers=auth(desk_token))
    token = sign_in(client, owner)

    by_desk = client.get("/audit-log", params={"actor_id": str(desk.id), "action": "create"}, headers=auth(token)).json()
    assert [(e["actor"]["name"], e["entity_type"]) for e in by_desk["items"]] == [("Desk", "patient"), ("Desk", "patient")]
    assert {p["name"] for p in by_desk["people"]} == {"Owner", "Desk"}

    history = client.get("/audit-log", params={"patient_id": first["id"]}, headers=auth(token)).json()["items"]
    assert [e["action"] for e in history] == ["view", "create"]
    assert history[0]["patient"] == {"id": first["id"], "code": first["code"], "name": "Asha Rao"}

    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert client.get("/audit-log", params={"date_from": tomorrow}, headers=auth(token)).json()["items"] == []

    page = client.get("/audit-log", params={"limit": 2}, headers=auth(token)).json()
    assert len(page["items"]) == 2 and page["next_before_id"] == page["items"][-1]["id"]
    older = client.get("/audit-log", params={"limit": 2, "before_id": page["next_before_id"]}, headers=auth(token)).json()
    assert all(e["id"] < page["next_before_id"] for e in older["items"])
