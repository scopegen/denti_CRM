from app.plans import PLANS, PlanLimits
from tests.conftest import add_picklist, auth, make_clinic, make_staff, sign_in

CONDITIONS = ["Diabetes", "Pregnancy"]


def _patient(name="Rahul Sharma", phone="98100 12345", **extra):
    body = {"name": name, "phone": phone, "birth_year": 1990}
    body.update(extra)
    return body


def _clinic_with_owner(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    add_picklist(clinic_id, "medical_condition", CONDITIONS)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    return clinic_id, owner


def test_register_numbers_patients_per_clinic(client):
    _, owner_a = _clinic_with_owner("alpha")
    _, owner_b = _clinic_with_owner("beta")
    token_a = sign_in(client, owner_a)
    token_b = sign_in(client, owner_b)

    first = client.post("/patients", json=_patient(), headers=auth(token_a)).json()
    second = client.post("/patients", json=_patient("Neha Singh", "98100 99999"), headers=auth(token_a)).json()
    other_clinic = client.post("/patients", json=_patient(), headers=auth(token_b)).json()

    assert (first["patient_number"], first["code"]) == (1, "ALPHA-0001")
    assert (second["patient_number"], second["code"]) == (2, "ALPHA-0002")
    assert (other_clinic["patient_number"], other_clinic["code"]) == (1, "BETA-0001")
    assert first["phone"] == "9810012345"


def test_same_phone_needs_confirmation(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    client.post("/patients", json=_patient(), headers=auth(token))

    again = client.post("/patients", json=_patient("Aarav Sharma"), headers=auth(token))
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "duplicate_phone"
    assert detail["matches"] == [{"code": "SMILE-0001", "name": "Rahul Sharma"}]

    confirmed = client.post("/patients", json=_patient("Aarav Sharma", allow_duplicate_phone=True), headers=auth(token))
    assert confirmed.status_code == 201


def test_date_of_birth_or_birth_year_is_required(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    response = client.post("/patients", json={"name": "No Age", "phone": "98100 11111"}, headers=auth(token))
    assert response.status_code == 422


def test_medical_conditions_must_come_from_the_clinic_list(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    ok = client.post("/patients", json=_patient(medical_conditions=["Diabetes"]), headers=auth(token))
    assert ok.status_code == 201
    bad = client.post(
        "/patients", json=_patient("X", "98100 22222", medical_conditions=["Made Up"]), headers=auth(token)
    )
    assert bad.status_code == 422


def test_search_by_name_phone_and_patient_id(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    client.post("/patients", json=_patient("Rahul Sharma", "98100 12345"), headers=auth(token))
    client.post("/patients", json=_patient("Neha Singh", "99990 55555"), headers=auth(token))

    def names(q):
        return [p["name"] for p in client.get("/patients", params={"q": q}, headers=auth(token)).json()["items"]]

    assert names("rahul") == ["Rahul Sharma"]
    assert names("55555") == ["Neha Singh"]
    assert names("SMILE-0002") == ["Neha Singh"]
    assert sorted(names("")) == ["Neha Singh", "Rahul Sharma"]


def test_list_is_paged(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    for i in range(3):
        client.post("/patients", json=_patient(f"Patient {i}", f"98100 0000{i}"), headers=auth(token))
    page = client.get("/patients", params={"page": 2, "page_size": 2}, headers=auth(token)).json()
    assert page["total"] == 3
    assert len(page["items"]) == 1


def test_plan_limit_counts_every_patient_ever_registered(client, monkeypatch):
    monkeypatch.setitem(PLANS, "basic", PlanLimits(receptionists=1, doctors=1, patients=2, storage_gb=3))
    _, owner = _clinic_with_owner(plan="basic")
    token = sign_in(client, owner)
    for i in range(2):
        assert client.post("/patients", json=_patient(f"P{i}", f"98100 0000{i}"), headers=auth(token)).status_code == 201

    over = client.post("/patients", json=_patient("P3", "98100 00003"), headers=auth(token))
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "plan_limit"
    stats = client.get("/patients/stats", headers=auth(token)).json()
    assert (stats["used"], stats["limit"], stats["total"]) == (2, 2, 2)


def test_opening_a_patient_is_logged(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    created = client.post("/patients", json=_patient(), headers=auth(token)).json()

    opened = client.get("/patients/SMILE-0001", headers=auth(token))
    assert opened.status_code == 200
    assert opened.json()["id"] == created["id"]

    log = client.get("/audit-log", headers=auth(token)).json()["items"]
    views = [e for e in log if e["action"] == "view" and e["patient"] and e["patient"]["id"] == created["id"]]
    assert len(views) == 1


def test_edit_records_what_changed(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    created = client.post("/patients", json=_patient(), headers=auth(token)).json()

    edited = client.patch(
        f"/patients/{created['id']}",
        json=_patient(phone="98100 12345", city="Noida", medical_conditions=["Pregnancy"]),
        headers=auth(token),
    )
    assert edited.status_code == 200
    assert edited.json()["city"] == "Noida"

    log = client.get("/audit-log", headers=auth(token)).json()["items"]
    update = next(e for e in log if e["action"] == "update" and e["entity_id"] == created["id"])
    assert update["changes"] == {"city": [None, "Noida"], "medical_conditions": [[], ["Pregnancy"]]}


def test_doctors_register_and_open_patients_too(client):
    clinic_id, _ = _clinic_with_owner()
    doctor = make_staff(clinic_id, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, doctor)
    assert client.post("/patients", json=_patient(), headers=auth(token)).status_code == 201
    assert client.get("/patients/SMILE-0001", headers=auth(token)).status_code == 200


def test_another_clinics_patient_cannot_be_opened_or_edited(client):
    _, owner_a = _clinic_with_owner("alpha")
    _, owner_b = _clinic_with_owner("beta")
    token_a = sign_in(client, owner_a)
    token_b = sign_in(client, owner_b)
    created_b = client.post("/patients", json=_patient(), headers=auth(token_b)).json()

    # Clinic A has no patient number 1, so B's patient 1 is simply not found.
    assert client.get("/patients/BETA-0001", headers=auth(token_a)).status_code == 404
    assert client.patch(f"/patients/{created_b['id']}", json=_patient(), headers=auth(token_a)).status_code == 404
    assert client.get("/patients", headers=auth(token_a)).json()["total"] == 0


def test_medical_conditions_list(client):
    _, owner = _clinic_with_owner()
    token = sign_in(client, owner)
    assert client.get("/picklists/medical_condition", headers=auth(token)).json() == CONDITIONS
