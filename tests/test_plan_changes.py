import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import engine
from tests.conftest import PASSWORD, auth, make_clinic, make_staff, owner_engine, sign_in


def _subscribe(clinic_id, cycle="monthly", days_used=15, days_left=15):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(
            text(
                "INSERT INTO subscriptions (clinic_id, billing_cycle, starter_pack_used, current_period_start, current_period_end) "
                "VALUES (:cid, :cycle, true, now() - make_interval(days => :used), now() + make_interval(days => :left))"
            ),
            {"cid": clinic_id, "cycle": cycle, "used": days_used, "left": days_left},
        )


def _sql(clinic_id, statement, **params):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(text(statement), params)


def _patient(client, token):
    return client.post("/patients", json={"name": "Rahul Sharma", "phone": "9810012345", "birth_year": 1990}, headers=auth(token)).json()


def test_moving_up_charges_the_difference_for_the_rest_of_the_period_and_applies_now(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _subscribe(clinic_id)
    token = sign_in(client, owner)

    preview = client.get("/plan/changes/preview?to=pro", headers=auth(token)).json()
    # Pro is ₹1,000 a month more; half the month is left: ₹500 plus 18% GST.
    assert (preview["kind"], preview["amount_paise"], preview["gst_paise"], preview["total_paise"]) == ("upgrade", 50000, 9000, 59000)
    assert preview["next_price_paise"] == 249900
    assert preview["payment_method"] == "test"
    assert 'Remove "Powered by Denti" from documents' in preview["gained"]

    changed = client.post("/plan/changes", json={"to_plan": "pro"}, headers=auth(token))
    assert changed.status_code == 200, changed.text
    assert changed.json()["plan"] == "pro"
    assert changed.json()["pending_change"] is None
    assert client.get("/auth/me", headers=auth(token)).json()["clinic"]["plan"] == "pro"
    payment = _sql(clinic_id, "SELECT total_paise, payment_method, status FROM plan_changes").one()
    assert tuple(payment) == (59000, "test", "applied")


def test_moving_up_switches_paused_logins_back_on(client):
    clinic_id = make_clinic("smile", plan="basic")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    make_staff(clinic_id, "smile", "doc1@smile.test", "doctor")
    paused = make_staff(clinic_id, "smile", "doc2@smile.test", "doctor", status="paused")
    _subscribe(clinic_id)
    token = sign_in(client, owner)

    client.post("/plan/changes", json={"to_plan": "standard"}, headers=auth(token))
    assert _sql(clinic_id, "SELECT status FROM staff WHERE id = :id", id=paused.id).scalar() == "active"
    assert sign_in(client, paused)


def test_moving_down_waits_for_the_paid_period_and_pauses_the_logins_not_kept(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    desk = make_staff(clinic_id, "smile", "desk@smile.test", "admin")
    first = make_staff(clinic_id, "smile", "doc1@smile.test", "doctor")
    second = make_staff(clinic_id, "smile", "doc2@smile.test", "doctor")
    third = make_staff(clinic_id, "smile", "doc3@smile.test", "doctor")
    _subscribe(clinic_id)
    token = sign_in(client, owner)
    patient = _patient(client, token)

    preview = client.get("/plan/changes/preview?to=basic", headers=auth(token)).json()
    assert preview["kind"] == "downgrade"
    assert preview["total_paise"] == 0
    assert [(s["role"], s["limit"], len(s["active"])) for s in preview["seats"]] == [("doctor", 1, 3)]
    assert preview["seats"][0]["suggested_keep"] == [str(first.id)]
    assert "Tooth chart" in preview["lost"]

    missing = client.post("/plan/changes", json={"to_plan": "basic"}, headers=auth(token))
    assert missing.status_code == 422
    assert missing.json()["detail"] == "Basic includes 1 doctor login. Choose 1 to keep."
    scheduled = client.post("/plan/changes", json={"to_plan": "basic", "keep_staff_ids": [str(second.id)]}, headers=auth(token)).json()
    assert scheduled["plan"] == "standard"
    assert (scheduled["pending_change"]["to_plan"], scheduled["pending_change"]["kind"]) == ("basic", "downgrade")

    # The receptionist sees the plan and the scheduled move, but cannot change either.
    desk_token = sign_in(client, desk)
    seen = client.get("/plan", headers=auth(desk_token)).json()
    assert (seen["pending_change"]["to_plan_name"], seen["can_change"]) == ("Basic", False)
    assert client.post("/plan/changes", json={"to_plan": "pro"}, headers=auth(desk_token)).status_code == 403
    third_token = sign_in(client, third)

    # The paid period ends.
    _sql(clinic_id, "UPDATE plan_changes SET effective_at = now() - interval '1 minute'")
    _sql(clinic_id, "UPDATE subscriptions SET current_period_start = now() - interval '1 month', current_period_end = now() - interval '1 minute'")
    after = client.get("/plan", headers=auth(token)).json()
    assert after["plan"] == "basic"
    assert after["pending_change"] is None
    statuses = dict(_sql(clinic_id, "SELECT email, status FROM staff WHERE role = 'doctor'").all())
    assert statuses == {"doc1@smile.test": "paused", "doc2@smile.test": "active", "doc3@smile.test": "paused"}
    # A new period on the lower plan starts where the paid one ended.
    assert _sql(clinic_id, "SELECT current_period_end > now() FROM subscriptions").scalar() is True

    # Paused people are signed out and cannot sign in; nothing they recorded is touched.
    assert client.get("/auth/me", headers=auth(third_token)).status_code == 401
    refused = client.post("/auth/login", json={"clinic": "smile", "email": third.email, "password": PASSWORD})
    assert refused.status_code == 403
    assert "paused" in refused.json()["detail"]
    assert client.get(f"/patients/{patient['id']}/tooth-chart", headers=auth(token)).status_code == 403


def test_a_scheduled_move_down_can_be_cancelled_or_replaced(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _subscribe(clinic_id)
    token = sign_in(client, owner)

    scheduled = client.post("/plan/changes", json={"to_plan": "basic"}, headers=auth(token)).json()
    cancelled = client.post(f"/plan/changes/{scheduled['pending_change']['id']}/cancel", headers=auth(token)).json()
    assert (cancelled["plan"], cancelled["pending_change"]) == ("standard", None)

    client.post("/plan/changes", json={"to_plan": "basic"}, headers=auth(token))
    # Moving up instead replaces the waiting move down.
    upgraded = client.post("/plan/changes", json={"to_plan": "pro"}, headers=auth(token)).json()
    assert (upgraded["plan"], upgraded["pending_change"]) == ("pro", None)
    assert sorted(_sql(clinic_id, "SELECT status FROM plan_changes").scalars().all()) == ["applied", "cancelled", "cancelled"]
    assert client.get("/plan/changes/preview?to=pro", headers=auth(token)).status_code == 400


def test_a_live_server_does_not_move_up_until_online_payment_is_connected(client, monkeypatch):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    _subscribe(clinic_id)
    token = sign_in(client, owner)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "payment_backend", None)

    refused = client.post("/plan/changes", json={"to_plan": "pro"}, headers=auth(token))
    assert refused.status_code == 503
    assert client.get("/plan", headers=auth(token)).json()["plan"] == "standard"
    # Moving down costs nothing, so it still works.
    assert client.post("/plan/changes", json={"to_plan": "basic"}, headers=auth(token)).status_code == 200


def test_a_paused_login_comes_back_when_a_seat_is_free(client):
    clinic_id = make_clinic("smile", plan="basic")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    active = make_staff(clinic_id, "smile", "doc1@smile.test", "doctor")
    paused = make_staff(clinic_id, "smile", "doc2@smile.test", "doctor", status="paused")
    token = sign_in(client, owner)

    full = client.post(f"/staff/{paused.id}/resume", headers=auth(token))
    assert full.status_code == 409
    assert full.json()["detail"] == "Your Basic plan includes 1 doctor login. Upgrade your plan to add more."
    client.post(f"/staff/{active.id}/deactivate", headers=auth(token))
    resumed = client.post(f"/staff/{paused.id}/resume", headers=auth(token))
    assert resumed.json()["status"] == "active"
    assert client.post(f"/staff/{active.id}/resume", headers=auth(token)).status_code == 409


def test_the_app_cannot_change_a_plan_except_through_a_paid_or_due_change(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    with engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(text("UPDATE clinics SET plan = 'pro'"))
        change = connection.execute(
            text(
                "INSERT INTO plan_changes (id, clinic_id, kind, from_plan, to_plan, effective_at, amount_paise, gst_paise, "
                "total_paise, requested_by) VALUES (gen_random_uuid(), :cid, 'upgrade', 'standard', 'pro', now(), 50000, 9000, "
                "59000, :by) RETURNING id"
            ),
            {"cid": clinic_id, "by": owner.id},
        ).scalar()
        with pytest.raises(DBAPIError, match="upgrade is not paid"):
            with connection.begin_nested():
                connection.execute(text("SELECT apply_plan_change(:id)"), {"id": change})
    assert _sql(clinic_id, "SELECT plan FROM clinics").scalar() == "standard"
