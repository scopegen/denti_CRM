import getpass

from sqlalchemy import text

from app import create_clinic
from tests.conftest import auth, last_code_for, owner_engine

ARGS = [
    "--slug", "ranco-dental", "--name", "Ranco Dental Clinic", "--prefix", "RANCO",
    "--owner-name", "Dr Ravi Kumar", "--owner-email", "Owner@RancoDental.in",
]


def _answers(monkeypatch, *passwords):
    given = iter(passwords)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(given))


def test_a_new_clinic_starts_with_its_owner_lists_services_and_starter_pack(client, monkeypatch, capsys):
    # Too short, then two that differ, then a good one typed twice.
    _answers(monkeypatch, "short", "FirstTry123", "Different123", "RancoOwner2026", "RancoOwner2026")
    assert create_clinic.main(ARGS) == 0
    assert "Sign in at http://localhost:5180/ranco-dental as owner@rancodental.in." in capsys.readouterr().out

    with owner_engine.begin() as connection:
        clinic_id, plan, prefix = connection.execute(
            text("SELECT id, plan, patient_id_prefix FROM clinics WHERE slug = 'ranco-dental'")
        ).one()
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM staff), (SELECT count(*) FROM services), "
                "(SELECT count(*) FROM picklists WHERE list = 'payment_mode'), (SELECT billing_cycle FROM subscriptions)"
            )
        ).one()
    assert (plan, prefix) == ("standard", "RANCO")
    assert counts[0] == 1 and counts[1] > 0 and counts[2] == 3 and counts[3] == "starter"

    signed_in = client.post(
        "/auth/login", json={"clinic": "ranco-dental", "email": "owner@rancodental.in", "password": "RancoOwner2026"}
    )
    challenge = signed_in.json()["challenge_id"]
    session = client.post(
        "/auth/verify", json={"clinic": "ranco-dental", "challenge_id": challenge, "code": last_code_for("owner@rancodental.in")}
    ).json()
    me = client.get("/auth/me", headers=auth(session["access_token"])).json()
    assert (me["staff"]["role"], me["staff"]["treats_patients"], me["clinic"]["name"]) == ("owner", True, "Ranco Dental Clinic")


def test_bad_details_or_a_taken_address_change_nothing(monkeypatch, capsys):
    assert create_clinic.main(["--slug", "API", "--name", "X", "--owner-name", "Y", "--owner-email", "nope"]) == 1
    out = capsys.readouterr().out
    assert "small letters" in out and "does not look right" in out

    _answers(monkeypatch, "RancoOwner2026", "RancoOwner2026")
    assert create_clinic.main(ARGS) == 0
    assert create_clinic.main(ARGS) == 1
    assert "already uses the address 'ranco-dental'" in capsys.readouterr().out
