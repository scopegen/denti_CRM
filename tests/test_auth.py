from sqlalchemy import text

from app import email
from tests.conftest import PASSWORD, auth, last_code_for, make_clinic, make_staff, owner_engine, sign_in


def _login(client, person, password=PASSWORD):
    return client.post("/auth/login", json={"clinic": person.clinic_slug, "email": person.email, "password": password})


def test_password_then_emailed_code_signs_in(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    step_one = _login(client, owner)
    assert step_one.status_code == 200
    body = step_one.json()
    assert body["status"] == "code_sent"
    assert body["sent_to"] == "o****@smile.test"
    assert len(email.outbox) == 1

    step_two = client.post(
        "/auth/verify",
        json={"clinic": "smile", "challenge_id": body["challenge_id"], "code": last_code_for(owner.email)},
    )
    assert step_two.status_code == 200
    token = step_two.json()["access_token"]

    me = client.get("/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["staff"]["email"] == "owner@smile.test"
    assert me.json()["clinic"]["slug"] == "smile"


def test_wrong_password_and_unknown_email_get_the_same_answer(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    wrong_password = _login(client, owner, password="nope")
    unknown = client.post("/auth/login", json={"clinic": "smile", "email": "nobody@smile.test", "password": PASSWORD})
    assert wrong_password.status_code == unknown.status_code == 401
    assert wrong_password.json()["detail"] == unknown.json()["detail"] == "Incorrect email or password."
    assert email.outbox == []


def test_no_session_without_the_code(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _login(client, owner)
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/refresh", json={"clinic": "smile"}).status_code == 401


def test_five_wrong_codes_cancel_the_code(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    challenge = _login(client, owner).json()["challenge_id"]
    real = last_code_for(owner.email)
    wrong = "000000" if real != "000000" else "111111"

    for tries_left in (4, 3, 2, 1):
        response = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": wrong})
        assert response.status_code == 400
        assert f"{tries_left} tr" in response.json()["detail"]
    fifth = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": wrong})
    assert fifth.json()["detail"] == "Too many wrong codes. Please request a new one."

    # Even the right code no longer works.
    late = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": real})
    assert late.status_code == 400


def test_expired_code_is_refused(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    challenge = _login(client, owner).json()["challenge_id"]
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(text("UPDATE login_codes SET expires_at = now() - interval '1 minute'"))

    response = client.post(
        "/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": last_code_for(owner.email)}
    )
    assert response.status_code == 400


def test_a_code_works_only_once(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    challenge = _login(client, owner).json()["challenge_id"]
    code = last_code_for(owner.email)
    first = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": code})
    second = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": challenge, "code": code})
    assert first.status_code == 200
    assert second.status_code == 400


def test_resend_waits_30_seconds_then_only_the_new_code_works(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    first = _login(client, owner).json()["challenge_id"]
    old_code = last_code_for(owner.email)

    too_soon = client.post("/auth/resend", json={"clinic": "smile", "challenge_id": first})
    assert too_soon.status_code == 429

    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(text("UPDATE login_codes SET created_at = now() - interval '31 seconds'"))
    resent = client.post("/auth/resend", json={"clinic": "smile", "challenge_id": first})
    assert resent.status_code == 200
    second = resent.json()["challenge_id"]
    new_code = last_code_for(owner.email)

    assert client.post("/auth/verify", json={"clinic": "smile", "challenge_id": first, "code": old_code}).status_code == 400
    assert client.post("/auth/verify", json={"clinic": "smile", "challenge_id": second, "code": new_code}).status_code == 200


def test_trusted_device_skips_the_code(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    sign_in(client, owner, trust_device=True)
    emails_so_far = len(email.outbox)

    again = _login(client, owner)
    assert again.json()["status"] == "signed_in"
    assert len(email.outbox) == emails_so_far


def test_refresh_keeps_the_person_signed_in_and_logout_ends_it(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    sign_in(client, owner)

    refreshed = client.post("/auth/refresh", json={"clinic": "smile"})
    assert refreshed.status_code == 200
    assert client.get("/auth/me", headers=auth(refreshed.json()["access_token"])).status_code == 200

    assert client.post("/auth/logout", json={"clinic": "smile"}).status_code == 204
    assert client.post("/auth/refresh", json={"clinic": "smile"}).status_code == 401


def test_deactivated_person_cannot_sign_in(client):
    clinic_id = make_clinic("smile")
    make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    gone = make_staff(clinic_id, "smile", "gone@smile.test", "doctor", status="deactivated")
    assert _login(client, gone).status_code == 401


def test_unknown_clinic(client):
    response = client.post("/auth/login", json={"clinic": "nowhere", "email": "a@b.test", "password": PASSWORD})
    assert response.status_code == 404
    assert client.get("/auth/clinic/nowhere").status_code == 404


def test_sign_in_page_gets_only_the_clinic_name(client):
    make_clinic("smile")
    assert client.get("/auth/clinic/smile").json() == {"slug": "smile", "name": "Smile Dental"}


def test_codes_are_stored_hashed(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _login(client, owner)
    code = last_code_for(owner.email)
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        stored = connection.execute(text("SELECT code_hash FROM login_codes")).scalar_one()
    assert code not in stored
    assert len(stored) == 64
