from datetime import date, timedelta

from sqlalchemy import text

from tests.conftest import add_picklist, auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile"):
    clinic_id = make_clinic(slug)
    add_picklist(clinic_id, "payment_mode", ["Cash", "Card", "UPI"])
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _patient(client, token, name="Rahul Sharma", phone="9810012345"):
    return client.post("/patients", json={"name": name, "phone": phone, "birth_year": 1990}, headers=auth(token)).json()


def _consult(client, token, patient_id, doctor_id):
    body = {"doctor_id": str(doctor_id), "chief_complaint": "Pain", "oral_examination": "Caries on 36"}
    assert client.post(f"/patients/{patient_id}/consultations", json=body, headers=auth(token)).status_code == 201


def _rct(client, token, patient_id, doctor_id):
    service = client.post(
        "/services", json={"name": "Root canal treatment", "kind": "in_house", "listed_price_paise": 650000}, headers=auth(token)
    ).json()
    t = client.post(
        f"/patients/{patient_id}/treatments",
        json={"service_id": service["id"], "doctor_id": str(doctor_id), "teeth": [36], "start": True},
        headers=auth(token),
    ).json()
    return t


def _pay(client, token, patient_id, amount, mode="UPI", **extra):
    return client.post(f"/patients/{patient_id}/payments", json={"amount_paise": amount, "payment_mode": mode, **extra}, headers=auth(token))


def test_the_bill_shows_every_charge_with_a_running_balance(client):
    _, owner, desk, doctor = _setup()
    owner_token = sign_in(client, owner)
    patient = _patient(client, owner_token)
    _consult(client, owner_token, patient["id"], doctor.id)
    t = _rct(client, owner_token, patient["id"], doctor.id)
    client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 650000, "discount_type": "percent", "discount_value": 1000},
        headers=auth(owner_token),
    )

    bill = client.get(f"/patients/{patient['id']}/bill", headers=auth(sign_in(client, desk))).json()
    lines = [(e["kind"], e["description"], e["amount_paise"], e["balance_paise"]) for e in bill["entries"]]
    assert lines == [
        ("consultation_fee", "Consultation fee", 50000, 50000),
        ("treatment_charge", "Root canal treatment, tooth 36", 650000, 700000),
        ("price_change", "Root canal treatment, tooth 36: Discount 10%", -65000, 635000),
    ]
    assert (bill["outstanding_paise"], bill["charged_paise"], bill["paid_paise"]) == (635000, 635000, 0)
    assert bill["payment_modes"] == ["Cash", "Card", "UPI"]


def test_doctors_cannot_see_or_change_the_bill(client):
    _, owner, _, doctor = _setup()
    patient = _patient(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    assert client.get(f"/patients/{patient['id']}/bill", headers=auth(token)).status_code == 403
    assert _pay(client, token, patient["id"], 100).status_code == 403
    assert client.get("/dues", headers=auth(token)).status_code == 403


def test_a_payment_brings_the_balance_down(client):
    clinic_id, owner, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    _consult(client, token, patient["id"], doctor.id)

    paid = _pay(client, token, patient["id"], 30000, note="Paid at the desk")
    assert paid.status_code == 201, paid.text
    bill = paid.json()
    assert bill["outstanding_paise"] == 20000
    assert bill["paid_paise"] == 30000
    last = bill["entries"][-1]
    assert (last["kind"], last["description"], last["amount_paise"], last["note"]) == ("payment", "Payment, UPI", -30000, "Paid at the desk")

    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        logged = connection.execute(text("SELECT count(*) FROM audit_log WHERE entity_type = 'ledger_entry'")).scalar()
    assert logged == 1


def test_payments_need_a_known_mode_a_real_amount_and_no_future_date(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    assert _pay(client, token, patient["id"], 100, mode="Cheque").status_code == 400
    assert _pay(client, token, patient["id"], 0).status_code == 422
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert _pay(client, token, patient["id"], 100, paid_on=tomorrow).status_code == 422

    last_week = (date.today() - timedelta(days=7)).isoformat()
    backdated = _pay(client, token, patient["id"], 100, paid_on=last_week).json()
    assert backdated["entries"][-1]["effective_at"].startswith(last_week)


def test_a_wrong_payment_is_reversed_not_edited(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    _consult(client, token, patient["id"], doctor.id)
    wrong = _pay(client, token, patient["id"], 5000000, mode="Card").json()["entries"][-1]

    reversed_bill = client.post(f"/ledger/{wrong['id']}/reverse", json={"reason": "Typed an extra zero"}, headers=auth(token)).json()
    assert reversed_bill["outstanding_paise"] == 50000
    kinds = [(e["kind"], e["amount_paise"], e["reversed"]) for e in reversed_bill["entries"]]
    assert kinds == [("consultation_fee", 50000, False), ("payment", -5000000, True), ("payment_reversal", 5000000, False)]
    assert reversed_bill["entries"][-1]["reverses_entry_id"] == wrong["id"]

    again = client.post(f"/ledger/{wrong['id']}/reverse", json={"reason": "Twice"}, headers=auth(token))
    assert again.status_code == 409
    charge = reversed_bill["entries"][0]
    assert client.post(f"/ledger/{charge['id']}/reverse", json={"reason": "No"}, headers=auth(token)).status_code == 404
    assert client.post(f"/ledger/{wrong['id']}/reverse", json={"reason": "  "}, headers=auth(token)).status_code == 422


def test_a_refund_comes_only_from_credit(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    _consult(client, token, patient["id"], doctor.id)
    no_credit = client.post(f"/patients/{patient['id']}/refunds", json={"amount_paise": 100, "payment_mode": "Cash"}, headers=auth(token))
    assert no_credit.status_code == 409

    _pay(client, token, patient["id"], 80000)  # Paid 800 against 500: 300 in credit.
    too_much = client.post(f"/patients/{patient['id']}/refunds", json={"amount_paise": 30001, "payment_mode": "Cash"}, headers=auth(token))
    assert too_much.status_code == 409
    assert "₹300" in too_much.json()["detail"]
    refunded = client.post(f"/patients/{patient['id']}/refunds", json={"amount_paise": 30000, "payment_mode": "Cash"}, headers=auth(token)).json()
    assert refunded["outstanding_paise"] == 0
    assert refunded["entries"][-1]["description"] == "Refund, Cash"


def test_an_opening_balance_is_added_once(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    first = client.post(f"/patients/{patient['id']}/opening-balance", json={"amount_paise": 250000, "note": "From Ranco"}, headers=auth(token))
    assert first.status_code == 201
    bill = first.json()
    assert (bill["outstanding_paise"], bill["has_opening_balance"]) == (250000, True)
    assert bill["entries"][0]["description"] == "Opening balance"
    second = client.post(f"/patients/{patient['id']}/opening-balance", json={"amount_paise": 100}, headers=auth(token))
    assert second.status_code == 409

    other = _patient(client, token, name="Asha Rao", phone="9810099999")
    credit = client.post(f"/patients/{other['id']}/opening-balance", json={"amount_paise": -40000}, headers=auth(token)).json()
    assert (credit["outstanding_paise"], credit["entries"][0]["description"]) == (-40000, "Opening credit")
    zero = client.post(f"/patients/{other['id']}/opening-balance", json={"amount_paise": 0}, headers=auth(token))
    assert zero.status_code == 422


def test_dues_list_who_owes_most_first(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    small = _patient(client, token, name="Asha Rao", phone="9810000001")
    big = _patient(client, token, name="Vikram Singh", phone="9810000002")
    paid_up = _patient(client, token, name="Meera Iyer", phone="9810000003")
    for p in (small, big, paid_up):
        _consult(client, token, p["id"], doctor.id)
    _rct(client, token, big["id"], doctor.id)
    _pay(client, token, paid_up["id"], 50000)
    _pay(client, token, small["id"], 10000)

    dues = client.get("/dues", headers=auth(token)).json()
    assert [(d["patient"]["name"], d["outstanding_paise"]) for d in dues["items"]] == [("Vikram Singh", 700000), ("Asha Rao", 40000)]
    assert (dues["count"], dues["total_paise"]) == (2, 740000)
    assert dues["items"][1]["last_payment_at"] is not None

    found = client.get("/dues", params={"q": "asha"}, headers=auth(token)).json()
    assert [d["patient"]["name"] for d in found["items"]] == ["Asha Rao"]
    by_code = client.get("/dues", params={"q": big["code"]}, headers=auth(token)).json()
    assert [d["patient"]["name"] for d in by_code["items"]] == ["Vikram Singh"]


def test_another_clinic_cannot_see_the_bill(client):
    _, _, desk, doctor = _setup("smile")
    token = sign_in(client, desk)
    patient = _patient(client, token)
    _consult(client, token, patient["id"], doctor.id)

    _, _, stranger, _ = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/bill", headers=auth(other)).status_code == 404
    assert _pay(client, other, patient["id"], 100).status_code == 404
    assert client.get("/dues", headers=auth(other)).json()["items"] == []


def test_a_clinic_without_its_own_payment_modes_still_takes_payments(client):
    # A clinic created before payment modes were a list had none, and could not take a payment.
    clinic_id = make_clinic("plain")
    desk = make_staff(clinic_id, "plain", "desk@plain.test", "admin")
    token = sign_in(client, desk)
    patient = _patient(client, token)

    bill = client.get(f"/patients/{patient['id']}/bill", headers=auth(token)).json()
    assert bill["payment_modes"] == ["Cash", "Card", "UPI"]
    assert _pay(client, token, patient["id"], 50000, mode="Cash").status_code == 201
    assert _pay(client, token, patient["id"], 50000, mode="Cheque").status_code == 400
