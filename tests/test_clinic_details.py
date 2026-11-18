import base64
import io

from PIL import Image
from sqlalchemy import text

from app.db import SessionLocal, use_clinic
from app.documents import letterhead_for
from app.models import Clinic
from app.pdf import Letterhead, invoice_html, letterhead_preview_html, letterhead_preview_pdf
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in

DETAILS = {
    "tagline": "Gentle care for the whole family",
    "address": "  Shop 4, Sector 18 \n\n Noida, Uttar Pradesh 201301 ",
    "phone": "1000000001",
    "email": "hello@smile.test",
    "website": "smile.test",
    "gstin": " 07abcde1234f1z5 ",
    "footer_text": "Open every day, 10 am to 8 pm",
}


def _owner(slug: str, plan: str = "standard"):
    clinic = make_clinic(slug, plan=plan)
    return clinic, make_staff(clinic, slug, f"owner@{slug}.test", "owner")


def _png(width: int = 200, height: int = 100, border: int = 20) -> str:
    """A red block inside a transparent border, as a data URL."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.paste((200, 30, 30, 255), (border, border, width - border, height - border))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def test_the_owner_saves_the_clinics_details_and_everyone_can_read_them(client):
    clinic, owner = _owner("smile")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, owner)

    saved = client.put(
        "/clinic/details",
        json={**DETAILS, "brand_colour": "#2F6FA3", "letterhead_design": "band"},
        headers=auth(token),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    # Tidied on the way in: blank lines and spaces gone, the GSTIN and colour in one form.
    assert body["address"] == "Shop 4, Sector 18\nNoida, Uttar Pradesh 201301"
    assert (body["gstin"], body["brand_colour"], body["letterhead_design"]) == ("07ABCDE1234F1Z5", "#2f6fa3", "band")
    assert (body["can_brand"], body["can_hide_powered_by"], body["has_logo"]) == (True, False, False)

    doctor_token = sign_in(client, doctor)
    read = client.get("/clinic/details", headers=auth(doctor_token)).json()
    assert read["tagline"] == DETAILS["tagline"] and read["phone"] == "1000000001"
    assert client.put("/clinic/details", json=DETAILS, headers=auth(doctor_token)).status_code == 403

    # Every change is in the activity log.
    log = client.get("/audit-log", headers=auth(token)).json()
    assert any(entry.get("entity_type") == "clinic" for entry in log.get("items", log))


def test_details_are_checked_before_they_are_saved(client):
    _, owner = _owner("smile")
    token = sign_in(client, owner)
    bad_gstin = client.put("/clinic/details", json={"gstin": "07ABCDE1234"}, headers=auth(token))
    assert bad_gstin.status_code == 422 and "15 characters" in bad_gstin.text
    long_address = client.put("/clinic/details", json={"address": "1\n2\n3\n4"}, headers=auth(token))
    assert long_address.status_code == 422 and "3 lines" in long_address.text
    assert client.put("/clinic/details", json={"brand_colour": "blue"}, headers=auth(token)).status_code == 422
    assert client.put("/clinic/details", json={"preprinted_top_margin_mm": 300}, headers=auth(token)).status_code == 422


def test_each_plan_gets_its_own_letterhead_features(client):
    _, basic_owner = _owner("basicclinic", plan="basic")
    basic = sign_in(client, basic_owner)
    assert client.put("/clinic/details", json={"brand_colour": "#2f6fa3"}, headers=auth(basic)).status_code == 403
    assert client.put("/clinic/details", json={"letterhead_design": "centred"}, headers=auth(basic)).status_code == 403
    assert client.put("/clinic/details", json={"hide_powered_by": True}, headers=auth(basic)).status_code == 403
    plain = client.put("/clinic/details", json={**DETAILS, "preprinted_letterhead": True}, headers=auth(basic))
    assert plain.status_code == 200 and plain.json()["can_brand"] is False

    _, standard_owner = _owner("standardclinic")
    standard = sign_in(client, standard_owner)
    assert client.put("/clinic/details", json={"hide_powered_by": True}, headers=auth(standard)).status_code == 403

    _, pro_owner = _owner("proclinic", plan="pro")
    pro = sign_in(client, pro_owner)
    full = client.put(
        "/clinic/details",
        json={**DETAILS, "brand_colour": "#6a55b8", "letterhead_design": "centred", "hide_powered_by": True},
        headers=auth(pro),
    )
    assert full.status_code == 200 and full.json()["can_hide_powered_by"] is True


def test_a_logo_is_added_shown_to_everyone_replaced_and_removed(client):
    clinic, owner = _owner("smile")
    doctor = make_staff(clinic, "smile", "doc@smile.test", "doctor")
    token = sign_in(client, owner)
    assert client.get("/clinic/logo", headers=auth(token)).status_code == 404

    added = client.put("/clinic/logo", json={"image_data": _png()}, headers=auth(token))
    assert added.status_code == 200, added.text
    first = added.json()["logo_version"]
    assert added.json()["has_logo"] is True and first

    logo = client.get("/clinic/logo", headers=auth(sign_in(client, doctor)))
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    # The transparent border is trimmed away.
    assert Image.open(io.BytesIO(logo.content)).size == (160, 60)

    replaced = client.put("/clinic/logo", json={"image_data": _png(120, 120, 0)}, headers=auth(token)).json()
    assert replaced["has_logo"] is True and replaced["logo_version"] != first
    assert client.put("/clinic/logo", json={"image_data": "data:image/png;base64,bm90IGFuIGltYWdl"}, headers=auth(token)).status_code == 422

    removed = client.delete("/clinic/logo", headers=auth(token)).json()
    assert (removed["has_logo"], removed["logo_version"]) == (False, None)
    assert client.get("/clinic/logo", headers=auth(token)).status_code == 404


def test_documents_print_the_letterhead_and_a_smaller_plan_prints_it_plain(client):
    clinic, owner = _owner("proclinic", plan="pro")
    doctor = make_staff(clinic, "proclinic", "doc@proclinic.test", "doctor")
    token = sign_in(client, owner)
    client.put("/clinic/logo", json={"image_data": _png()}, headers=auth(token))
    client.put(
        "/clinic/details",
        json={**DETAILS, "brand_colour": "#6a55b8", "letterhead_design": "band", "hide_powered_by": True},
        headers=auth(token),
    )

    preview = client.get("/clinic/letterhead/preview", headers=auth(token))
    assert preview.status_code == 200 and preview.content.startswith(b"%PDF")
    assert client.get("/clinic/letterhead/preview", headers=auth(sign_in(client, doctor))).status_code == 403

    with SessionLocal() as db:
        use_clinic(db, clinic)
        letterhead = letterhead_for(db, db.get(Clinic, clinic))
    assert letterhead.lines == [
        "Shop 4, Sector 18, Noida, Uttar Pradesh 201301", "1000000001 · hello@smile.test · smile.test", "GSTIN 07ABCDE1234F1Z5",
    ]
    assert (letterhead.accent, letterhead.design, letterhead.show_powered_by) == ("#6a55b8", "band", False)
    assert letterhead.logo_png and letterhead.footer_text == DETAILS["footer_text"]
    html = letterhead_preview_html(letterhead, None)
    assert "GSTIN 07ABCDE1234F1Z5" in html and "Powered by Denti" not in html and "#6a55b8" in html

    # Moving to Basic keeps the details but prints the plain letterhead with Powered by Denti.
    with owner_engine.begin() as connection:
        connection.execute(text("UPDATE clinics SET plan = 'basic' WHERE id = :id"), {"id": clinic})
    with SessionLocal() as db:
        use_clinic(db, clinic)
        plain = letterhead_for(db, db.get(Clinic, clinic))
    assert (plain.accent, plain.design, plain.show_powered_by) == (None, "classic", True)
    assert plain.lines == letterhead.lines


def test_every_design_and_preprinted_paper_render():
    png = base64.b64decode(_png().split(",", 1)[1])
    for design in ("classic", "centred", "band"):
        letterhead = Letterhead(
            clinic_name="Smile Dental Clinic", tagline="Gentle care", lines=["Sector 18, Noida", "1000000001"],
            logo_png=png, accent="#2f6fa3", design=design, footer_text="Open every day",
        )
        assert letterhead_preview_pdf(letterhead, None).startswith(b"%PDF")
    paper = Letterhead(clinic_name="Smile Dental Clinic", preprinted=True, top_margin_mm=50)
    html = letterhead_preview_html(paper, None)
    # Nothing printed at the top, and the page starts 5 cm down.
    assert "header_content" not in html.split("<body>", 1)[1] and "top: 5.0cm" in html
    assert letterhead_preview_pdf(paper, None).startswith(b"%PDF")
    assert "Powered by Denti" in invoice_html(paper, _invoice_doc())


def _invoice_doc():
    from app.pdf import InvoiceDoc

    return InvoiceDoc(
        number_text="SMILE/2026/0001", date_text="17 Nov 2026", patient_name="Test Patient", patient_code="SMILE-0001",
        phone="1000000001", lines=[], total="0",
    )
