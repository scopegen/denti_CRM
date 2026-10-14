import base64
import io

from PIL import Image, ImageDraw
from sqlalchemy import text

from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile"):
    clinic_id = make_clinic(slug)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    reception = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, reception, doctor


def _signature_photo() -> str:
    """Dark ink on off white paper, the way a phone photo of a signature looks."""
    image = Image.new("RGB", (1200, 400), (246, 244, 240))
    draw = ImageDraw.Draw(image)
    draw.line([(150, 250), (400, 120), (650, 280), (1000, 140)], fill=(20, 30, 80), width=14)
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def _files(clinic_id):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(text("SELECT kind, deleted_at, purge_after FROM files ORDER BY uploaded_at")).all()


def test_a_doctor_uploads_their_signature_and_it_is_cleaned(client):
    clinic_id, _, _, doctor = _setup()
    token = sign_in(client, doctor)

    saved = client.put(f"/staff/{doctor.id}/signature", json={"image_data": _signature_photo()}, headers=auth(token))
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_signature"] is True

    image = client.get(f"/staff/{doctor.id}/signature", headers=auth(token))
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    png = Image.open(io.BytesIO(image.content))
    assert png.mode == "RGBA"
    # The paper is gone: corners are see through, and the image is cropped to the ink.
    assert png.getpixel((0, 0))[3] < 255
    assert png.width < 1000
    assert [kind for kind, *_ in _files(clinic_id)] == ["signature"]


def test_replacing_a_signature_moves_the_old_one_to_the_bin(client):
    clinic_id, owner, _, doctor = _setup()
    token = sign_in(client, owner)
    client.put(f"/staff/{doctor.id}/signature", json={"image_data": _signature_photo()}, headers=auth(token))
    client.put(f"/staff/{doctor.id}/signature", json={"image_data": _signature_photo()}, headers=auth(token))

    files = _files(clinic_id)
    assert len(files) == 2
    old, new = files
    assert old.deleted_at is not None
    assert (old.purge_after - old.deleted_at).days == 30
    assert new.deleted_at is None

    removed = client.delete(f"/staff/{doctor.id}/signature", headers=auth(token))
    assert removed.json()["has_signature"] is False
    assert all(f.deleted_at is not None for f in _files(clinic_id))
    assert client.get(f"/staff/{doctor.id}/signature", headers=auth(token)).status_code == 404


def test_a_doctor_cannot_change_another_doctors_signature(client):
    clinic_id, owner, _, doctor = _setup()
    other = make_staff(clinic_id, "smile", "other@smile.test", "doctor")
    token = sign_in(client, doctor)
    response = client.put(f"/staff/{other.id}/signature", json={"image_data": _signature_photo()}, headers=auth(token))
    assert response.status_code == 403
    assert client.get(f"/staff/{owner.id}/signature", headers=auth(token)).status_code == 403


def test_a_receptionist_who_does_not_treat_patients_has_no_signature(client):
    _, _, reception, _ = _setup()
    token = sign_in(client, reception)
    response = client.put(f"/staff/{reception.id}/signature", json={"image_data": _signature_photo()}, headers=auth(token))
    assert response.status_code == 400


def test_an_unreadable_or_blank_signature_is_refused(client):
    _, _, _, doctor = _setup()
    token = sign_in(client, doctor)
    garbage = client.put(
        f"/staff/{doctor.id}/signature", json={"image_data": "data:image/png;base64,bm90IGFuIGltYWdlIGF0IGFsbA=="}, headers=auth(token)
    )
    assert garbage.status_code == 422
    assert "could not be read" in garbage.json()["detail"]

    blank = io.BytesIO()
    Image.new("RGB", (300, 100), (255, 255, 255)).save(blank, format="PNG")
    empty = client.put(
        f"/staff/{doctor.id}/signature",
        json={"image_data": "data:image/png;base64," + base64.b64encode(blank.getvalue()).decode()},
        headers=auth(token),
    )
    assert empty.status_code == 422
    assert "empty" in empty.json()["detail"]


def _consult(client, token, doctor_id):
    patient = client.post(
        "/patients", json={"name": "Rahul Sharma", "phone": "1010012345", "birth_year": 1990, "gender": "male"}, headers=auth(token)
    ).json()
    body = {
        "doctor_id": str(doctor_id),
        "chief_complaint": "Pain in lower left molar",
        "oral_examination": "Deep caries on 36",
        "prescription": {
            "diagnosis": "Irreversible pulpitis",
            "advice": "Warm saline rinses",
            "items": [{"medicine": "Amoxicillin 500 mg", "frequency": "TDS", "duration": "5 days"}],
        },
    }
    consultation = client.post(f"/patients/{patient['id']}/consultations", json=body, headers=auth(token)).json()
    return patient, consultation


def test_the_prescription_pdf_prints_the_doctor_and_their_signature(client):
    clinic_id, owner, _, doctor = _setup()
    doctor_token = sign_in(client, doctor)
    client.put(f"/staff/{doctor.id}/signature", json={"image_data": _signature_photo()}, headers=auth(doctor_token))
    patient, consultation = _consult(client, doctor_token, doctor.id)
    prescription_id = consultation["prescription"]["id"]

    response = client.get(f"/prescriptions/{prescription_id}/pdf", headers=auth(doctor_token))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert patient["code"] in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")
    # The signature is drawn as an image on the page.
    assert b"/Subtype /Image" in response.content or b"/Subtype/Image" in response.content

    download = client.get(f"/prescriptions/{prescription_id}/pdf?download=true", headers=auth(doctor_token))
    assert download.headers["content-disposition"].startswith("attachment;")

    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        actions = connection.execute(
            text("SELECT action FROM audit_log WHERE entity_type = 'prescription' ORDER BY occurred_at")
        ).scalars().all()
    assert actions == ["view", "download"]


def test_an_earlier_version_of_a_prescription_can_be_printed(client):
    _, _, _, doctor = _setup()
    token = sign_in(client, doctor)
    _, consultation = _consult(client, token, doctor.id)
    prescription_id = consultation["prescription"]["id"]
    edited = {
        "doctor_id": str(doctor.id),
        "chief_complaint": "Pain in lower left molar",
        "oral_examination": "Deep caries on 36",
        "prescription": {"diagnosis": "Irreversible pulpitis", "items": [{"medicine": "Ibuprofen 400 mg", "frequency": "SOS"}]},
    }
    client.patch(f"/consultations/{consultation['id']}", json=edited, headers=auth(token))

    assert client.get(f"/prescriptions/{prescription_id}/pdf?version=1", headers=auth(token)).status_code == 200
    assert client.get(f"/prescriptions/{prescription_id}/pdf?version=2", headers=auth(token)).status_code == 200
    assert client.get(f"/prescriptions/{prescription_id}/pdf?version=3", headers=auth(token)).status_code == 404


def test_a_prescription_from_another_clinic_cannot_be_printed(client):
    _, _, _, doctor = _setup("smile")
    token = sign_in(client, doctor)
    _, consultation = _consult(client, token, doctor.id)

    _, _, _, stranger = _setup("bright")
    other_token = sign_in(client, stranger)
    response = client.get(f"/prescriptions/{consultation['prescription']['id']}/pdf", headers=auth(other_token))
    assert response.status_code == 404


def test_the_prescription_layout_has_no_dashes_and_hides_empty_sections():
    from app.pdf import Doctor, Letterhead, PrescriptionDoc, prescription_html, prescription_pdf

    doc = PrescriptionDoc(
        date_text="28 Sep 2026", patient_name="Rahul Sharma", patient_code="SMILE-0001", age_gender="36 years / Male",
        phone="1010012345", doctor=Doctor(name="Dr Rohan Mehta", qualification="BDS"),
        diagnosis="Pulpitis", items=[{"medicine": "Amoxicillin 500 mg", "frequency": "TDS", "duration": "5 days"}],
    )
    html = prescription_html(Letterhead(clinic_name="Smile Dental Clinic"), doc)
    for dash in ("\N{EN DASH}", "\N{EM DASH}", "-" * 2):
        assert dash not in html
    assert "TDS (three times a day)" in html
    assert "Powered by Denti" in html
    # Nothing recorded, nothing printed: no empty headings or placeholders.
    assert "Chief complaint" not in html
    assert "Advice" not in html
    assert "Reg. No." not in html

    hidden = prescription_html(Letterhead(clinic_name="Smile Dental Clinic", show_powered_by=False), doc)
    assert "Powered by Denti" not in hidden
    assert prescription_pdf(Letterhead(clinic_name="Smile Dental Clinic"), doc).startswith(b"%PDF")
