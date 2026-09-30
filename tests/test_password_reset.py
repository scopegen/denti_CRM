from sqlalchemy import text

from app import email
from tests.conftest import PASSWORD, auth, last_code_for, make_clinic, make_staff, owner_engine, sign_in

NEW_PASSWORD = "BatteryStaple9"


def _start(client, address, clinic="smile"):
    response = client.post("/auth/password-reset", json={"clinic": clinic, "email": address})
    assert response.status_code == 200, response.text
    return response.json()["challenge_id"]


def _finish(client, challenge, code, password=NEW_PASSWORD, clinic="smile"):
    return client.post(
        "/auth/password-reset/confirm",
        json={"clinic": clinic, "challenge_id": challenge, "code": code, "new_password": password},
    )


def _login(client, person, password):
    return client.post("/auth/login", json={"clinic": person.clinic_slug, "email": person.email, "password": password})


def _audit_actions(clinic_id, staff_id):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return [
            row.action
            for row in connection.execute(
                text("SELECT action FROM audit_log WHERE entity_id = :id ORDER BY id"), {"id": staff_id}
            )
        ]


def test_a_forgotten_password_is_reset_with_an_emailed_code(client):
    clinic_id = make_clinic("smile")
    doctor = make_staff(clinic_id, "smile", "doctor@smile.test", "doctor")

    challenge = _start(client, "Doctor@Smile.test")
    assert email.outbox[-1].subject.startswith("Your Denti password reset code:")
    assert _finish(client, challenge, last_code_for(doctor.email)).status_code == 204
    assert email.outbox[-1].subject == "Your Denti password was changed"

    assert _login(client, doctor, PASSWORD).status_code == 401
    assert _login(client, doctor, NEW_PASSWORD).json()["status"] == "code_sent"
    assert _audit_actions(clinic_id, doctor.id)[:2] == ["password_reset_code_sent", "password_reset"]


def test_an_unknown_email_gets_the_same_answer_and_no_email(client):
    clinic_id = make_clinic("smile")
    make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    response = client.post("/auth/password-reset", json={"clinic": "smile", "email": "nobody@smile.test"})
    assert response.status_code == 200
    assert set(response.json()) == {"challenge_id", "resend_after_seconds"}
    assert email.outbox == []
    # Its challenge leads nowhere.
    assert _finish(client, response.json()["challenge_id"], "123456").status_code == 400


def test_a_deactivated_login_gets_no_reset_code(client):
    clinic_id = make_clinic("smile")
    make_staff(clinic_id, "smile", "gone@smile.test", "doctor", status="deactivated")
    _start(client, "gone@smile.test")
    assert email.outbox == []


def test_a_reset_signs_out_every_device_and_forgets_trusted_ones(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner, trust_device=True)
    assert client.get("/auth/me", headers=auth(token)).status_code == 200

    challenge = _start(client, owner.email)
    assert _finish(client, challenge, last_code_for(owner.email)).status_code == 204

    assert client.get("/auth/me", headers=auth(token)).status_code == 401
    assert client.post("/auth/refresh", json={"clinic": "smile"}).status_code == 401
    # The trusted browser has to enter a code again.
    assert _login(client, owner, NEW_PASSWORD).json()["status"] == "code_sent"


def test_a_reset_code_cannot_sign_in_and_a_sign_in_code_cannot_reset(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    reset_challenge = _start(client, owner.email)
    reset_code = last_code_for(owner.email)
    as_sign_in = client.post("/auth/verify", json={"clinic": "smile", "challenge_id": reset_challenge, "code": reset_code})
    assert as_sign_in.status_code == 400

    sign_in_challenge = _login(client, owner, PASSWORD).json()["challenge_id"]
    assert _finish(client, sign_in_challenge, last_code_for(owner.email)).status_code == 400
    # Asking to sign in did not cancel the reset code.
    assert _finish(client, reset_challenge, reset_code).status_code == 204


def test_five_wrong_reset_codes_cancel_it_and_a_code_works_once(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    challenge = _start(client, owner.email)
    real = last_code_for(owner.email)
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(4):
        assert "left" in _finish(client, challenge, wrong).json()["detail"]
    assert _finish(client, challenge, wrong).json()["detail"] == "Too many wrong codes. Please request a new one."
    assert _finish(client, challenge, real).status_code == 400

    again = _start(client, owner.email)
    code = last_code_for(owner.email)
    assert _finish(client, again, code).status_code == 204
    assert _finish(client, again, code, password="AnotherOne77").status_code == 400
    assert _login(client, owner, "AnotherOne77").status_code == 401


def test_the_new_password_needs_eight_characters(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    challenge = _start(client, owner.email)
    assert _finish(client, challenge, last_code_for(owner.email), password="short").status_code == 422


def test_too_many_requests_send_nothing_more_but_the_newest_code_still_works(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    for _ in range(5):
        challenge = _start(client, owner.email)
    code = last_code_for(owner.email)
    sent = len(email.outbox)

    same = _start(client, owner.email)
    assert len(email.outbox) == sent
    assert same == challenge
    assert _finish(client, same, code).status_code == 204


def test_a_signed_in_person_changes_their_password_and_other_devices_are_signed_out(client):
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    other_device = sign_in(client, owner, trust_device=True)
    client.cookies.clear()
    this_device = sign_in(client, owner)

    wrong = client.post(
        "/auth/password", json={"current_password": "nope", "new_password": NEW_PASSWORD}, headers=auth(this_device)
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "Your current password is not right."
    same = client.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD}, headers=auth(this_device)
    )
    assert same.status_code == 400

    changed = client.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}, headers=auth(this_device)
    )
    assert changed.status_code == 200
    fresh = changed.json()["access_token"]
    assert email.outbox[-1].subject == "Your Denti password was changed"

    assert client.get("/auth/me", headers=auth(fresh)).status_code == 200
    assert client.get("/auth/me", headers=auth(other_device)).status_code == 401
    # This browser's session carries on.
    assert client.post("/auth/refresh", json={"clinic": "smile"}).status_code == 200
    assert _login(client, owner, NEW_PASSWORD).status_code == 200
    assert "password_changed" in _audit_actions(clinic_id, owner.id)
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        trusted = connection.scalar(text("SELECT count(*) FROM trusted_devices WHERE revoked_at IS NULL"))
    assert trusted == 0
