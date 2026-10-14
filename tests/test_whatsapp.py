from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from app.whatsapp import whatsapp_number
from tests.conftest import add_picklist, auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(plan="standard", slug="smile"):
    clinic_id = make_clinic(slug, plan=plan)
    add_picklist(clinic_id, "payment_mode", ["Cash", "UPI"])
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _patient(client, token, consent=True, phone="10600 12345"):
    body = {"name": "Rohan Das", "phone": phone, "birth_year": 1980, "whatsapp_consent": consent}
    return client.post("/patients", json=body, headers=auth(token)).json()


def _open(client, token, **body):
    return client.post("/whatsapp/messages", json=body, headers=auth(token))


def _text(response):
    """The message as WhatsApp will show it, read back from the wa.me address."""
    url = urlparse(response.json()["url"])
    return url.netloc + url.path, parse_qs(url.query)["text"][0]


def _link_token(message):
    return message.split("/d/")[1].split()[0]


def test_phone_numbers_become_whatsapp_numbers():
    assert whatsapp_number("10600 12345") == "911060012345"
    assert whatsapp_number("+91 10600 12345") == "911060012345"
    assert whatsapp_number("010600 12345") == "911060012345"
    assert whatsapp_number("+44 7700 900123") == "447700900123"
    assert whatsapp_number("12345") is None


def test_a_visit_reminder_opens_whatsapp_with_the_message_written(client):
    clinic_id, _, desk, _ = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    due = (date.today() + timedelta(days=2)).isoformat()
    follow_up = client.post(
        f"/patients/{patient['id']}/follow-ups",
        json={"kind": "visit", "due_on": due, "due_time": "11:30", "reason": "Root canal sitting 2"},
        headers=auth(token),
    ).json()

    opened = _open(client, token, patient_id=patient["id"], kind="visit_reminder", follow_up_id=follow_up["id"])
    assert opened.status_code == 200, opened.text
    where, message = _text(opened)
    assert where == "wa.me/911060012345"
    assert message.startswith("Hello Rohan, this is Smile Dental. A reminder of your dental visit on ")
    assert "at 11:30 AM" in message
    assert opened.json()["link_expires_at"] is None

    log = client.get(f"/patients/{patient['id']}/messages", headers=auth(token)).json()
    assert [(m["kind"], m["sent_by"]["name"], m["link"]) for m in log] == [("visit_reminder", "Desk", None)]

    # A call follow up needs the call message, and a done follow up needs none.
    wrong = _open(client, token, patient_id=patient["id"], kind="call_reminder", follow_up_id=follow_up["id"])
    assert wrong.status_code == 422
    client.post(f"/follow-ups/{follow_up['id']}/done", headers=auth(token))
    assert _open(client, token, patient_id=patient["id"], kind="visit_reminder", follow_up_id=follow_up["id"]).status_code == 409


def test_no_message_without_consent_and_none_on_basic(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token, consent=False)
    refused = _open(client, token, patient_id=patient["id"], kind="general")
    assert refused.status_code == 409
    assert "has not agreed to WhatsApp messages" in refused.json()["detail"]

    _, _, basic_desk, _ = _setup(plan="basic", slug="basic")
    basic_token = sign_in(client, basic_desk)
    basic_patient = _patient(client, basic_token)
    assert _open(client, basic_token, patient_id=basic_patient["id"], kind="general").status_code == 403
    assert client.get("/whatsapp/settings", headers=auth(basic_token)).status_code == 403


def test_the_clinic_changes_its_wording_and_language(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)

    bad = client.put("/whatsapp/templates/general/en", json={"body": "Hi {name}, your bill is {amount}"}, headers=auth(token))
    assert bad.status_code == 422
    assert bad.json()["detail"].startswith("{amount} cannot be used in this message.")
    no_link = client.put("/whatsapp/templates/invoice/en", json={"body": "Hi {name}, your invoice is ready."}, headers=auth(token))
    assert no_link.json()["detail"] == "This message must keep {link}, or the patient cannot open it."

    saved = client.put("/whatsapp/templates/general/en", json={"body": "Namaste {name}! Team {clinic} here."}, headers=auth(token))
    general = next(t for t in saved.json()["templates"] if (t["kind"], t["language"]) == ("general", "en"))
    assert (general["body"], general["is_default"]) == ("Namaste Rohan! Team Smile Dental here.".replace("Rohan", "{name}").replace("Smile Dental", "{clinic}"), False)
    assert _text(_open(client, token, patient_id=patient["id"], kind="general"))[1] == "Namaste Rohan! Team Smile Dental here."

    client.put("/whatsapp/settings", json={"language": "hi"}, headers=auth(token))
    assert _text(_open(client, token, patient_id=patient["id"], kind="general"))[1] == "नमस्ते Rohan, Smile Dental से संदेश।"
    # One message can still go in English.
    english = _open(client, token, patient_id=patient["id"], kind="general", language="en")
    assert _text(english)[1] == "Namaste Rohan! Team Smile Dental here."

    reset = client.delete("/whatsapp/templates/general/en", headers=auth(token)).json()
    general = next(t for t in reset["templates"] if (t["kind"], t["language"]) == ("general", "en"))
    assert general["is_default"] is True

    # Doctors send messages but do not change the wording.
    doctor_token = sign_in(client, doctor)
    assert client.put("/whatsapp/templates/general/en", json={"body": "Hi {name}"}, headers=auth(doctor_token)).status_code == 403


def test_a_prescription_goes_as_a_link_the_patient_opens_without_signing_in(client):
    clinic_id, _, desk, doctor = _setup()
    desk_token = sign_in(client, desk)
    patient = _patient(client, desk_token)
    doctor_token = sign_in(client, doctor)
    consultation = client.post(
        f"/patients/{patient['id']}/consultations",
        json={
            "doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Caries on 36",
            "prescription": {"diagnosis": "Pulpitis", "advice": "Rinse", "items": [{"medicine": "Amoxicillin 500 mg"}]},
        },
        headers=auth(doctor_token),
    ).json()
    prescription_id = consultation["prescription"]["id"]

    opened = _open(client, doctor_token, patient_id=patient["id"], kind="prescription", prescription_id=prescription_id)
    assert opened.status_code == 200, opened.text
    message = _text(opened)[1]
    assert message.startswith("Hello Rohan, here is your prescription from Doc at Smile Dental: http://localhost:5180/smile/d/")
    token = _link_token(message)

    client.cookies.clear()
    info = client.get(f"/public/smile/documents/{token}")
    assert info.status_code == 200
    assert (info.json()["clinic_name"], info.json()["title"]) == ("Smile Dental", "Prescription")
    pdf = client.get(f"/public/smile/documents/{token}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")

    # The link does not work under another clinic's address, or with a changed token.
    _setup(slug="other")
    assert client.get(f"/public/other/documents/{token}").status_code == 404
    assert client.get(f"/public/smile/documents/{token[:-2]}xx").status_code == 404

    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        opened_by_patient = connection.execute(
            text("SELECT action, entity_type FROM audit_log WHERE actor_type = 'patient'")
        ).all()
    assert opened_by_patient == [("view", "prescription")]

    log = client.get(f"/patients/{patient['id']}/messages", headers=auth(desk_token)).json()
    assert log[0]["link"]["open_count"] == 1
    assert log[0]["link"]["active"] is True


def test_a_stopped_or_expired_link_no_longer_opens(client):
    clinic_id, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    service = client.post("/services", json={"name": "Scaling", "kind": "in_house", "listed_price_paise": 150000}, headers=auth(token)).json()
    client.post(
        f"/patients/{patient['id']}/treatments",
        json={"service_id": service["id"], "doctor_id": str(doctor.id), "teeth": [], "start": True},
        headers=auth(token),
    )
    invoices = client.get(f"/patients/{patient['id']}/invoices", headers=auth(token)).json()
    created = client.post(
        f"/patients/{patient['id']}/invoices",
        json={"consultation_ids": [], "treatment_ids": [i["id"] for i in invoices["invoiceable"]]},
        headers=auth(token),
    ).json()
    invoice = created["invoices"][0]

    first = _text(_open(client, token, patient_id=patient["id"], kind="invoice", invoice_id=invoice["id"]))[1]
    assert f"invoice {invoice['number_text']} from Smile Dental for ₹1,500:" in first
    second = _text(_open(client, token, patient_id=patient["id"], kind="invoice", invoice_id=invoice["id"]))[1]
    first_token, second_token = _link_token(first), _link_token(second)

    log = client.get(f"/patients/{patient['id']}/messages", headers=auth(token)).json()
    stopped = client.post(f"/share-links/{log[1]['link']['id']}/stop", headers=auth(token))
    assert stopped.json()["active"] is False
    gone = client.get(f"/public/smile/documents/{first_token}/pdf")
    assert gone.status_code == 410
    assert gone.json()["detail"] == "The clinic has stopped this link. Please ask them to send it again."

    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(text("UPDATE share_links SET expires_at = now() - interval '1 minute'"))
    assert client.get(f"/public/smile/documents/{second_token}").json()["detail"] == (
        "This link has expired. Please ask the clinic to send it again."
    )


def test_doctors_cannot_send_invoices_or_estimates_or_see_those_messages(client):
    _, _, desk, doctor = _setup()
    desk_token = sign_in(client, desk)
    patient = _patient(client, desk_token)
    services = {}
    for name, price in (("Root canal treatment", 650000), ("Ceramic crown", 900000)):
        services[name] = client.post("/services", json={"name": name, "kind": "in_house", "listed_price_paise": price}, headers=auth(desk_token)).json()
    estimate = client.post(
        f"/patients/{patient['id']}/estimates",
        json={
            "doctor_id": str(doctor.id),
            "options": [{"label": "Option A", "items": [
                {"service_id": services["Root canal treatment"]["id"], "teeth": [36], "sittings": 3, "unit_price_paise": 650000},
            ]}],
        },
        headers=auth(desk_token),
    ).json()[0]
    sent = _open(client, desk_token, patient_id=patient["id"], kind="estimate", estimate_id=estimate["id"])
    assert "It is valid until" in _text(sent)[1]

    doctor_token = sign_in(client, doctor)
    assert _open(client, doctor_token, patient_id=patient["id"], kind="estimate", estimate_id=estimate["id"]).status_code == 403
    seen = client.get(f"/patients/{patient['id']}/messages", headers=auth(doctor_token)).json()
    assert seen[0]["body"] == "Invoice or estimate sent on WhatsApp."
    assert seen[0]["link"] is None


def test_a_review_request_needs_a_finished_treatment_and_the_review_link(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    patient = _patient(client, token)
    service = client.post("/services", json={"name": "Scaling", "kind": "in_house", "listed_price_paise": 150000}, headers=auth(token)).json()
    treatment = client.post(
        f"/patients/{patient['id']}/treatments",
        json={"service_id": service["id"], "doctor_id": str(doctor.id), "teeth": [], "start": True},
        headers=auth(token),
    ).json()

    early = _open(client, token, patient_id=patient["id"], kind="review_request", treatment_id=treatment["id"])
    assert early.json()["detail"] == "Ask for a review once the treatment is finished."
    client.post(f"/treatments/{treatment['id']}/finish", json={}, headers=auth(token))
    no_link = _open(client, token, patient_id=patient["id"], kind="review_request", treatment_id=treatment["id"])
    assert no_link.json()["detail"] == "Add your clinic's Google review link in Settings, WhatsApp first."

    bad = client.put("/whatsapp/settings", json={"language": "en", "google_review_url": "g.page/smile"}, headers=auth(token))
    assert bad.status_code == 422
    client.put("/whatsapp/settings", json={"language": "en", "google_review_url": "https://g.page/r/smile/review"}, headers=auth(token))
    asked = _open(client, token, patient_id=patient["id"], kind="review_request", treatment_id=treatment["id"])
    assert _text(asked)[1] == (
        "Hello Rohan, thank you for choosing Smile Dental. We hope your scaling went well. "
        "If you have a minute, we would be grateful for a review: https://g.page/r/smile/review"
    )
