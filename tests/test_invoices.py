from datetime import date

from app.routers.invoices import financial_year
from tests.conftest import add_picklist, auth, make_clinic, make_staff, sign_in


def _setup(slug="smile"):
    clinic_id = make_clinic(slug)
    add_picklist(clinic_id, "payment_mode", ["Cash", "UPI"])
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _billed_patient(client, token, doctor_id, discount=None):
    """A patient with a consultation fee (₹500), a started root canal (₹6,500) and a planned crown."""
    patient = client.post("/patients", json={"name": "Rohan Das", "phone": "1060012345", "birth_year": 1980}, headers=auth(token)).json()
    client.post(
        f"/patients/{patient['id']}/consultations",
        json={"doctor_id": str(doctor_id), "chief_complaint": "Pain", "oral_examination": "Caries"},
        headers=auth(token),
    )
    services = {}
    for name, price in (("Root canal treatment", 650000), ("Ceramic crown", 900000)):
        services[name] = client.post("/services", json={"name": name, "kind": "in_house", "listed_price_paise": price}, headers=auth(token)).json()
    rct = client.post(
        f"/patients/{patient['id']}/treatments",
        json={"service_id": services["Root canal treatment"]["id"], "doctor_id": str(doctor_id), "teeth": [36], "start": True},
        headers=auth(token),
    ).json()
    client.post(
        f"/patients/{patient['id']}/treatments",
        json={"service_id": services["Ceramic crown"]["id"], "doctor_id": str(doctor_id), "teeth": [36]},
        headers=auth(token),
    )
    if discount:
        client.put(
            f"/treatments/{rct['id']}/price",
            json={"price_paise": 650000, "discount_type": "percent", "discount_value": discount},
            headers=auth(token),
        )
    return patient


def _create(client, token, patient_id, items):
    body = {
        "consultation_ids": [i["id"] for i in items if i["kind"] == "consultation"],
        "treatment_ids": [i["id"] for i in items if i["kind"] == "treatment"],
    }
    return client.post(f"/patients/{patient_id}/invoices", json=body, headers=auth(token))


def test_the_financial_year_runs_from_april_to_march():
    assert financial_year(date(2026, 3, 31)) == "2025-26"
    assert financial_year(date(2026, 4, 1)) == "2026-27"
    assert financial_year(date(2099, 12, 31)) == "2099-00"


def test_an_invoice_lists_what_is_on_the_bill_at_the_final_price(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _billed_patient(client, token, doctor.id, discount=1000)

    start = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()
    assert start["invoices"] == []
    # The planned crown is not on the bill, so it cannot be invoiced.
    assert [(i["kind"], i["description"], i["amount_paise"]) for i in start["invoiceable"]] == [
        ("consultation", "Consultation, Doc", 50000),
        ("treatment", "Root canal treatment", 585000),
    ]

    created = _create(client, token, patient["id"], start["invoiceable"])
    assert created.status_code == 201, created.text
    invoice = created.json()["invoices"][0]
    fy = financial_year(date.today())
    assert invoice["number_text"] == f"SMILE/{fy[2:]}/0001"
    assert (invoice["total_paise"], invoice["status"]) == (635000, "issued")
    assert [(l["description"], l["teeth"], l["amount_paise"]) for l in invoice["lines"]] == [
        ("Consultation, Doc", [], 50000),
        ("Root canal treatment", [36], 585000),
    ]
    assert created.json()["invoiceable"] == []


def test_an_item_goes_on_one_invoice_until_that_invoice_is_cancelled(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _billed_patient(client, token, doctor.id)
    items = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()["invoiceable"]
    first = _create(client, token, patient["id"], items).json()["invoices"][0]

    twice = _create(client, token, patient["id"], items)
    assert twice.status_code == 409

    cancelled = client.post(f"/invoices/{first['id']}/cancel", json={"reason": "Wrong items"}, headers=auth(token)).json()
    assert cancelled["invoices"][0]["status"] == "cancelled"
    assert len(cancelled["invoiceable"]) == 2
    again = _create(client, token, patient["id"], cancelled["invoiceable"]).json()["invoices"]
    # The cancelled one keeps its number; the new one takes the next.
    assert [i["number_text"][-4:] for i in again] == ["0002", "0001"]
    assert client.post(f"/invoices/{first['id']}/cancel", json={"reason": "Again"}, headers=auth(token)).status_code == 409


def test_an_invoice_needs_items_and_no_future_date(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _billed_patient(client, token, doctor.id)
    assert _create(client, token, patient["id"], []).status_code == 422
    items = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()["invoiceable"]
    future = client.post(
        f"/patients/{patient['id']}/invoices",
        json={"consultation_ids": [items[0]["id"]], "issued_on": "2099-01-01"},
        headers=auth(token),
    )
    assert future.status_code == 422


def test_the_invoice_prints_as_a_pdf(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _billed_patient(client, token, doctor.id)
    client.post(f"/patients/{patient['id']}/payments", json={"amount_paise": 300000, "payment_mode": "UPI"}, headers=auth(token))
    items = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()["invoiceable"]
    invoice = _create(client, token, patient["id"], items).json()["invoices"][0]

    pdf = client.get(f"/invoices/{invoice['id']}/pdf?download=true", headers=auth(token))
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    fy = financial_year(date.today())
    assert f'Invoice SMILE-{fy[2:]}-0001.pdf' in pdf.headers["content-disposition"]

    client.post(f"/invoices/{invoice['id']}/cancel", json={"reason": "Wrong patient"}, headers=auth(token))
    assert client.get(f"/invoices/{invoice['id']}/pdf", headers=auth(token)).status_code == 200


def test_doctors_cannot_see_invoices(client):
    _, owner, _, doctor = _setup()
    patient = _billed_patient(client, sign_in(client, owner), doctor.id)
    token = sign_in(client, doctor)
    assert client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).status_code == 403


def test_another_clinic_cannot_see_the_invoices(client):
    _, _, desk, doctor = _setup("smile")
    token = sign_in(client, desk)
    patient = _billed_patient(client, token, doctor.id)
    items = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()["invoiceable"]
    invoice = _create(client, token, patient["id"], items).json()["invoices"][0]

    _, _, stranger, _ = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/invoices", headers=auth(other)).status_code == 404
    assert client.get(f"/invoices/{invoice['id']}/pdf", headers=auth(other)).status_code == 404
    assert client.post(f"/invoices/{invoice['id']}/cancel", json={"reason": "x"}, headers=auth(other)).status_code == 404
