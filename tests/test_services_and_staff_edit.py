from tests.conftest import auth, make_clinic, make_staff, sign_in


def _service(name="Root canal treatment", price=650000, **extra):
    body = {"name": name, "category": "Root canal", "kind": "in_house", "listed_price_paise": price}
    body.update(extra)
    return body


def test_admin_manages_the_catalog_and_everyone_reads_it(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")
    owner_token = sign_in(client, owner)

    created = client.post("/services", json=_service(), headers=auth(owner_token))
    assert created.status_code == 201
    assert created.json()["listed_price_paise"] == 650000

    doctor_token = sign_in(client, doctor)
    assert [s["name"] for s in client.get("/services", headers=auth(doctor_token)).json()] == ["Root canal treatment"]
    # Prices are the owner's and receptionist's business.
    assert client.post("/services", json=_service("Filling"), headers=auth(doctor_token)).status_code == 403


def test_service_names_are_unique_per_clinic(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    client.post("/services", json=_service(), headers=auth(token))
    again = client.post("/services", json=_service("ROOT CANAL TREATMENT"), headers=auth(token))
    assert again.status_code == 409


def test_retired_services_are_hidden_but_kept(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    service = client.post("/services", json=_service(), headers=auth(token)).json()

    retired = client.patch(f"/services/{service['id']}", json=_service(active=False), headers=auth(token))
    assert retired.json()["active"] is False
    assert client.get("/services", headers=auth(token)).json() == []
    assert len(client.get("/services", params={"include_retired": True}, headers=auth(token)).json()) == 1


def test_price_change_is_logged(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    service = client.post("/services", json=_service(), headers=auth(token)).json()
    client.patch(f"/services/{service['id']}", json=_service(price=700000), headers=auth(token))

    log = client.get("/audit-log", headers=auth(token)).json()["items"]
    change = next(e for e in log if e["action"] == "update" and e["entity_type"] == "service")
    assert change["changes"] == {"listed_price_paise": [650000, 700000]}


def test_prices_cannot_be_negative(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    assert client.post("/services", json=_service(price=-1), headers=auth(token)).status_code == 422


def test_edit_staff_details(client):
    clinic = make_clinic("smile")
    owner = make_staff(clinic, "smile", "owner@smile.test", "owner")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, owner)

    updated = client.patch(
        f"/staff/{doctor.id}",
        json={"name": "Dr Rohan Mehta", "qualification": "BDS", "registration_no": "A-20871"},
        headers=auth(token),
    )
    assert updated.status_code == 200
    assert updated.json()["registration_no"] == "A-20871"

    doctor_token = sign_in(client, doctor)
    assert client.patch(f"/staff/{owner.id}", json={"name": "X"}, headers=auth(doctor_token)).status_code == 403


def test_another_clinics_service_cannot_be_changed(client):
    a = make_clinic("alpha")
    b = make_clinic("beta")
    owner_a = make_staff(a, "alpha", "owner@alpha.test", "owner")
    owner_b = make_staff(b, "beta", "owner@beta.test", "owner")
    service_b = client.post("/services", json=_service(), headers=auth(sign_in(client, owner_b))).json()
    response = client.patch(f"/services/{service_b['id']}", json=_service(price=1), headers=auth(sign_in(client, owner_a)))
    assert response.status_code == 404
