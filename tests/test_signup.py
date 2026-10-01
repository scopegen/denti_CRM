from sqlalchemy import text

from app import email
from app.config import settings
from tests.conftest import auth, last_code_for, make_clinic, owner_engine

DETAILS = {
    "plan": "standard", "clinic_name": "Bright Smiles Dental", "slug": "bright-smiles", "patient_id_prefix": "bsd",
    "owner_name": "Dr Meera Nair", "email": "Meera@BrightSmiles.in", "password": "BrightSmiles2026",
}


def _start(client, **changes):
    return client.post("/signup", json={**DETAILS, **changes})


def _clinic_rows(slug):
    with owner_engine.begin() as connection:
        clinic = connection.execute(text("SELECT id, name, plan, patient_id_prefix FROM clinics WHERE slug = :s"), {"s": slug}).first()
        if clinic is None:
            return None, None
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic.id)})
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM staff WHERE role = 'owner'), (SELECT count(*) FROM services), "
                "(SELECT count(*) FROM picklists WHERE list = 'payment_mode'), "
                "(SELECT billing_cycle || ' ' || status FROM subscriptions)"
            )
        ).one()
    return clinic, counts


def test_a_clinic_signs_up_confirms_its_email_and_lands_signed_in(client):
    plans = client.get("/signup/plans").json()
    assert [p["key"] for p in plans["plans"]] == ["basic", "standard", "pro"]
    assert client.get("/signup/address?slug=bright-smiles").json()["available"] is True

    started = _start(client)
    assert started.status_code == 201, started.text
    body = started.json()
    assert (body["amount_paise"], body["gst_paise"], body["total_paise"]) == (80000, 14400, 94400)
    assert (body["sent_to"], body["payment_method"]) == ("m****@brightsmiles.in", "test")
    assert email.outbox[-1].subject.startswith("Your Denti sign up code:")
    # Nothing exists until the email is confirmed.
    assert _clinic_rows("bright-smiles") == (None, None)

    done = client.post(f"/signup/{body['signup_id']}/verify", json={"code": last_code_for("meera@brightsmiles.in")})
    assert done.status_code == 200, done.text
    result = done.json()
    assert (result["clinic_slug"], result["paid"]) == ("bright-smiles", True)

    me = client.get("/auth/me", headers=auth(result["session"]["access_token"])).json()
    assert (me["staff"]["email"], me["staff"]["role"], me["staff"]["treats_patients"]) == ("meera@brightsmiles.in", "owner", True)
    assert (me["clinic"]["name"], me["clinic"]["plan"]) == ("Bright Smiles Dental", "standard")
    clinic, counts = _clinic_rows("bright-smiles")
    assert clinic.patient_id_prefix == "BSD"
    assert counts[0] == 1 and counts[1] > 0 and counts[2] == 3 and counts[3] == "starter active"
    # The browser keeps the sign in, as after a normal sign in.
    assert client.post("/auth/refresh", json={"clinic": "bright-smiles"}).status_code == 200
    # The same sign up cannot be used twice, and the address is now taken.
    again = client.post(f"/signup/{body['signup_id']}/verify", json={"code": "000000"})
    assert again.status_code == 400
    assert client.get("/signup/address?slug=bright-smiles").json()["available"] is False


def test_taken_reserved_or_badly_written_addresses_are_refused(client):
    make_clinic("smile")
    assert client.get("/signup/address?slug=smile").json()["reason"].startswith("Another clinic already uses")
    assert client.get("/signup/address?slug=signup").json()["reason"].startswith("This address is used by Denti")
    assert client.get("/signup/address?slug=Bad Name").json()["available"] is False
    assert _start(client, slug="smile").status_code == 409
    assert _start(client, patient_id_prefix="TOO-LONG-PREFIX").status_code == 422
    assert _start(client, password="short").status_code == 422
    assert email.outbox == []


def test_wrong_codes_run_out_and_a_new_code_can_be_asked_for(client, monkeypatch):
    monkeypatch.setattr(settings, "login_code_resend_seconds", 0)
    signup_id = _start(client).json()["signup_id"]
    real = last_code_for("meera@brightsmiles.in")
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(4):
        assert "left" in client.post(f"/signup/{signup_id}/verify", json={"code": wrong}).json()["detail"]
    assert client.post(f"/signup/{signup_id}/verify", json={"code": wrong}).json()["detail"] == "Too many wrong codes. Please ask for a new one."
    assert client.post(f"/signup/{signup_id}/verify", json={"code": real}).status_code == 400

    assert client.post(f"/signup/{signup_id}/resend").status_code == 200
    fresh = last_code_for("meera@brightsmiles.in")
    assert client.post(f"/signup/{signup_id}/verify", json={"code": fresh}).status_code == 200


def test_too_many_sign_ups_from_one_email_are_stopped(client):
    for n in range(5):
        assert _start(client, slug=f"bright-{n}").status_code == 201
    stopped = _start(client, slug="bright-9")
    assert stopped.status_code == 429


def test_a_live_server_without_online_payment_creates_the_clinic_with_payment_due(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "payment_backend", None)
    body = _start(client).json()
    assert body["payment_method"] == "none"
    done = client.post(f"/signup/{body['signup_id']}/verify", json={"code": last_code_for("meera@brightsmiles.in")}).json()
    assert done["paid"] is False
    assert _clinic_rows("bright-smiles")[1][3] == "starter past_due"


def test_the_app_cannot_create_a_clinic_for_an_unconfirmed_sign_up(client):
    from sqlalchemy.exc import DBAPIError

    from app.db import engine

    signup_id = _start(client).json()["signup_id"]
    with engine.begin() as connection:
        try:
            connection.execute(text("SELECT create_signed_up_clinic(:id, true)"), {"id": signup_id})
            raised = None
        except DBAPIError as error:
            raised = str(error.orig)
    assert raised and "email not confirmed" in raised
    assert _clinic_rows("bright-smiles") == (None, None)
