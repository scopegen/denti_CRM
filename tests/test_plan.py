import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.plans import GB
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _subscribe(clinic_id, cycle="starter", packs=0):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(
            text(
                "INSERT INTO subscriptions (clinic_id, billing_cycle, starter_pack_used, current_period_start, "
                "current_period_end, storage_addon_packs) "
                "VALUES (:cid, :cycle, true, now(), now() + interval '3 months', :packs)"
            ),
            {"cid": clinic_id, "cycle": cycle, "packs": packs},
        )


def test_owner_sees_the_plan_and_how_much_is_used(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    make_staff(clinic_id, "smile", "desk@smile.test", "admin")
    make_staff(clinic_id, "smile", "doc1@smile.test", "doctor")
    make_staff(clinic_id, "smile", "doc2@smile.test", "doctor")
    make_staff(clinic_id, "smile", "gone@smile.test", "doctor", status="deactivated")
    _subscribe(clinic_id)
    token = sign_in(client, owner)
    for n in range(3):
        client.post("/patients", json={"name": f"Patient {n}", "phone": f"10100000{n}0", "birth_year": 1990}, headers=auth(token))

    body = client.get("/plan", headers=auth(token)).json()
    assert body["plan"] == "standard"
    assert body["plan_name"] == "Standard"
    usage = body["usage"]
    assert usage["patients"] == {"used": 3, "limit": 1500}
    assert usage["receptionists"] == {"used": 1, "limit": 1}
    # The owner is free and never counted; a deactivated login frees its seat.
    assert usage["doctors"] == {"used": 2, "limit": 5}
    assert usage["storage_bytes"] == {"used": 0, "limit": 15 * GB}
    assert body["billing"]["billing_cycle"] == "starter"
    assert body["gst_percent"] == 18


def test_prices_and_features_come_from_one_list(client):
    clinic_id = make_clinic("smile", plan="basic")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    body = client.get("/plan", headers=auth(sign_in(client, owner))).json()

    prices = {p["key"]: (p["starter_pack_paise"], p["monthly_paise"], p["yearly_paise"]) for p in body["plans"]}
    assert prices == {
        "basic": (60000, 99900, 999000),
        "standard": (80000, 149900, 1499000),
        "pro": (110000, 249900, 2499000),
    }
    pro = next(p for p in body["plans"] if p["key"] == "pro")
    assert (pro["patients"], pro["doctors"], pro["receptionists"], pro["storage_gb"]) == (None, None, None, 40)
    tooth_chart = next(f for f in body["features"] if f["label"] == "Tooth chart")
    assert tooth_chart["plans"] == ["standard", "pro"]
    assert (body["storage_pack_gb"], body["storage_pack_monthly_paise"]) == (50, 49900)
    # No subscription row yet: the plan still shows, without billing details.
    assert body["billing"] is None


def test_storage_packs_add_to_the_storage_limit(client):
    clinic_id = make_clinic("smile", plan="basic")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _subscribe(clinic_id, cycle="monthly", packs=2)
    body = client.get("/plan", headers=auth(sign_in(client, owner))).json()
    assert body["usage"]["storage_bytes"]["limit"] == (3 + 2 * 50) * GB
    assert body["billing"]["storage_addon_packs"] == 2


def test_pro_has_no_limits_on_people_or_patients(client):
    clinic_id = make_clinic("smile", plan="pro")
    reception = make_staff(clinic_id, "smile", "desk@smile.test", "admin")
    body = client.get("/plan", headers=auth(sign_in(client, reception))).json()
    assert body["usage"]["patients"]["limit"] is None
    assert body["usage"]["doctors"]["limit"] is None
    assert body["usage"]["receptionists"] == {"used": 1, "limit": None}


def test_doctors_cannot_see_the_plan(client):
    clinic_id = make_clinic("smile")
    doctor = make_staff(clinic_id, "smile", "doc@smile.test", "doctor")
    assert client.get("/plan", headers=auth(sign_in(client, doctor))).status_code == 403


def test_the_app_cannot_change_its_own_subscription():
    """Only payments and the platform change a subscription; the API role can only read it."""
    clinic_id = make_clinic("smile")
    _subscribe(clinic_id)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
            connection.execute(text("UPDATE subscriptions SET storage_addon_packs = 99"))


def test_one_clinic_cannot_read_another_clinics_subscription():
    smile = make_clinic("smile")
    bright = make_clinic("bright")
    _subscribe(smile)
    _subscribe(bright)
    with engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(smile)})
        rows = connection.execute(text("SELECT clinic_id FROM subscriptions")).scalars().all()
    assert rows == [smile]
