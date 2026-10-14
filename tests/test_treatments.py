from datetime import date, timedelta

from sqlalchemy import text

from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _patient(client, token, name="Rahul Sharma", phone="1010012345"):
    return client.post("/patients", json={"name": name, "phone": phone, "birth_year": 1990}, headers=auth(token)).json()


def _service(client, owner_token, name="Root canal treatment", price=650000, recall=None):
    body = {"name": name, "kind": "in_house", "listed_price_paise": price, "recall_after_months": recall}
    response = client.post("/services", json=body, headers=auth(owner_token))
    assert response.status_code == 201, response.text
    return response.json()


def _rows(clinic_id, sql, **params):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(text(sql), params).all()


def _bill(clinic_id):
    return [(kind, amount) for kind, amount in _rows(clinic_id, "SELECT kind, amount_paise FROM ledger_entries ORDER BY created_at, id")]


def _treatment(client, token, patient_id, service_id, doctor_id, **extra):
    body = {"service_id": service_id, "doctor_id": str(doctor_id), **extra}
    return client.post(f"/patients/{patient_id}/treatments", json=body, headers=auth(token))


def test_a_doctor_starts_a_treatment_that_no_consultation_recommended(client):
    clinic_id, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)

    started = _treatment(client, token, patient["id"], rct["id"], doctor.id, teeth=[36], start=True)
    assert started.status_code == 201, started.text
    body = started.json()
    assert (body["status"], body["service"]["name"], body["teeth"]) == ("ongoing", "Root canal treatment", [36])
    assert body["consultation_id"] is None
    # Doctors never see money.
    assert body["money"] is None
    assert _bill(clinic_id) == [("treatment_charge", 650000)]


def test_a_planned_treatment_is_not_billed_until_it_starts(client):
    clinic_id, owner, desk, doctor = _setup()
    owner_token = sign_in(client, owner)
    rct = _service(client, owner_token)
    token = sign_in(client, desk)
    patient = _patient(client, token)

    planned = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()
    assert planned["status"] == "planned"
    assert planned["money"]["billed_paise"] == 0
    assert _bill(clinic_id) == []

    started = client.post(f"/treatments/{planned['id']}/start", json={}, headers=auth(token)).json()
    assert started["status"] == "ongoing"
    assert started["money"]["billed_paise"] == 650000
    assert _bill(clinic_id) == [("treatment_charge", 650000)]


def test_discounts_change_the_bill_by_the_difference_only(client):
    """The design doc's example: root canal ₹6,500 with a 10% discount."""
    clinic_id, owner, desk, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, desk)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id, start=True).json()

    ten = client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 650000, "discount_type": "percent", "discount_value": 1000},
        headers=auth(token),
    ).json()
    assert ten["money"]["final_price_paise"] == 585000
    assert _bill(clinic_id) == [("treatment_charge", 650000), ("price_change", -65000)]

    twenty = client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 650000, "discount_type": "percent", "discount_value": 2000},
        headers=auth(token),
    ).json()
    assert twenty["money"]["final_price_paise"] == 520000
    assert _bill(clinic_id)[-1] == ("price_change", -65000)
    assert sum(amount for _, amount in _bill(clinic_id)) == 520000

    notes = [n for (n,) in _rows(clinic_id, "SELECT note FROM ledger_entries WHERE kind = 'price_change' ORDER BY created_at")]
    assert notes == ["Discount 10%", "Discount 20%"]


def test_a_price_set_before_starting_is_billed_when_it_starts(client):
    clinic_id, owner, desk, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, desk)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id, price_paise=600000).json()
    client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 600000, "adjustment_type": "amount", "adjustment_value": 50000,
              "discount_type": "amount", "discount_value": 20000},
        headers=auth(token),
    )
    assert _bill(clinic_id) == []
    client.post(f"/treatments/{t['id']}/start", json={}, headers=auth(token))
    assert _bill(clinic_id) == [("treatment_charge", 600000), ("price_change", 30000)]


def test_doctors_cannot_set_or_see_prices(client):
    _, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    with_price = _treatment(client, token, patient["id"], rct["id"], doctor.id, price_paise=1)
    assert with_price.status_code == 403
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()
    assert client.put(f"/treatments/{t['id']}/price", json={"price_paise": 1}, headers=auth(token)).status_code == 403

    services = client.get("/services", headers=auth(token)).json()
    assert [s["listed_price_paise"] for s in services] == [None]


def test_a_discount_cannot_be_more_than_the_price(client):
    _, owner, desk, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, desk)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()
    too_much = client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 1000, "discount_type": "amount", "discount_value": 1001},
        headers=auth(token),
    )
    assert too_much.status_code == 422
    over_all = client.put(
        f"/treatments/{t['id']}/price",
        json={"price_paise": 1000, "discount_type": "percent", "discount_value": 10001},
        headers=auth(token),
    )
    assert over_all.status_code == 422


def test_cancelling_a_started_treatment_takes_its_charge_off_the_bill(client):
    clinic_id, owner, desk, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    doctor_token = sign_in(client, doctor)
    patient = _patient(client, doctor_token)
    started = _treatment(client, doctor_token, patient["id"], rct["id"], doctor.id, start=True).json()

    # The doctor cannot: it changes the bill.
    refused = client.post(f"/treatments/{started['id']}/cancel", json={"reason": "Patient moved away"}, headers=auth(doctor_token))
    assert refused.status_code == 403

    desk_token = sign_in(client, desk)
    client.put(
        f"/treatments/{started['id']}/price",
        json={"price_paise": 650000, "discount_type": "percent", "discount_value": 1000},
        headers=auth(desk_token),
    )
    cancelled = client.post(f"/treatments/{started['id']}/cancel", json={"reason": "Patient moved away"}, headers=auth(desk_token)).json()
    assert (cancelled["status"], cancelled["cancel_reason"]) == ("cancelled", "Patient moved away")
    assert cancelled["money"]["billed_paise"] == 0
    assert _bill(clinic_id)[-1] == ("price_change", -585000)
    assert sum(amount for _, amount in _bill(clinic_id)) == 0

    again = client.post(f"/treatments/{started['id']}/cancel", json={"reason": "Twice"}, headers=auth(desk_token))
    assert again.status_code == 409


def test_a_doctor_can_cancel_a_planned_treatment(client):
    clinic_id, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()
    blank = client.post(f"/treatments/{t['id']}/cancel", json={"reason": "  "}, headers=auth(token))
    assert blank.status_code == 422
    cancelled = client.post(f"/treatments/{t['id']}/cancel", json={"reason": "Patient chose another option"}, headers=auth(token))
    assert cancelled.json()["status"] == "cancelled"
    assert _bill(clinic_id) == []


def test_a_visit_starts_a_planned_treatment_marks_the_patient_seen_and_books_the_next_sitting(client):
    clinic_id, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    client.post("/arrivals", json={"patient_id": patient["id"]}, headers=auth(token))
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id, teeth=[36]).json()

    next_week = (date.today() + timedelta(days=7)).isoformat()
    visited = client.post(
        f"/treatments/{t['id']}/visits",
        json={
            "doctor_id": str(doctor.id),
            "notes": "Access opening, working length taken",
            "prescription": {"items": [{"medicine": "Ibuprofen 400 mg", "frequency": "SOS"}]},
            "next_visit": {"due_on": next_week},
        },
        headers=auth(token),
    )
    assert visited.status_code == 201, visited.text
    body = visited.json()
    assert body["status"] == "ongoing"
    assert [v["notes"] for v in body["visits"]] == ["Access opening, working length taken"]
    assert body["visits"][0]["prescription"]["items"][0]["medicine"] == "Ibuprofen 400 mg"
    assert _bill(clinic_id) == [("treatment_charge", 650000)]

    arrived = client.get("/today", headers=auth(token)).json()["arrived"]
    assert arrived[0]["seen_at"] is not None
    follow_ups = client.get(f"/patients/{patient['id']}/follow-ups", headers=auth(token)).json()
    assert [(f["kind"], f["source"], f["reason"], f["due_on"]) for f in follow_ups] == [
        ("visit", "after_visit", "Next sitting: Root canal treatment", next_week)
    ]

    # The visit's prescription prints, naming the treatment.
    pdf = client.get(f"/prescriptions/{body['visits'][0]['prescription']['id']}/pdf", headers=auth(token))
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")


def test_the_last_sitting_finishes_the_treatment_and_books_its_recall(client):
    clinic_id, owner, _, doctor = _setup()
    scaling = _service(client, sign_in(client, owner), name="Scaling and polishing", price=150000, recall=6)
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], scaling["id"], doctor.id, start=True).json()

    finished = client.post(
        f"/treatments/{t['id']}/visits",
        json={"doctor_id": str(doctor.id), "notes": "Full mouth scaling", "finish_treatment": True},
        headers=auth(token),
    ).json()
    assert finished["status"] == "finished"
    assert finished["finished_on"] == date.today().isoformat()
    recalls = [f for f in client.get(f"/patients/{patient['id']}/follow-ups", headers=auth(token)).json() if f["source"] == "recall"]
    assert len(recalls) == 1
    assert recalls[0]["kind"] == "call"
    assert recalls[0]["reason"] == "Recall after Scaling and polishing"

    late = client.post(f"/treatments/{t['id']}/visits", json={"doctor_id": str(doctor.id)}, headers=auth(token))
    assert late.status_code == 409


def test_visit_dates_follow_the_treatment(client):
    _, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id, start=True).json()
    before_start = client.post(
        f"/treatments/{t['id']}/visits",
        json={"doctor_id": str(doctor.id), "visit_date": (date.today() - timedelta(days=3)).isoformat()},
        headers=auth(token),
    )
    assert before_start.status_code == 422
    future = client.post(
        f"/treatments/{t['id']}/visits",
        json={"doctor_id": str(doctor.id), "visit_date": (date.today() + timedelta(days=3)).isoformat()},
        headers=auth(token),
    )
    assert future.status_code == 422
    past_next = client.post(
        f"/treatments/{t['id']}/visits",
        json={"doctor_id": str(doctor.id), "next_visit": {"due_on": (date.today() - timedelta(days=1)).isoformat()}},
        headers=auth(token),
    )
    assert past_next.status_code == 422


def test_only_the_prescribing_doctor_changes_a_visit_prescription(client):
    clinic_id, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    other = make_staff(clinic_id, "smile", "other@smile.test", "doctor")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()
    visit = client.post(
        f"/treatments/{t['id']}/visits",
        json={"doctor_id": str(doctor.id), "prescription": {"items": [{"medicine": "Paracetamol 650 mg"}]}},
        headers=auth(token),
    ).json()["visits"][0]

    other_token = sign_in(client, other)
    changed = client.patch(
        f"/visits/{visit['id']}",
        json={"visit_date": visit["visit_date"], "doctor_id": str(doctor.id), "notes": "Edited",
              "prescription": {"items": [{"medicine": "Something else"}]}},
        headers=auth(other_token),
    )
    assert changed.status_code == 403
    notes_only = client.patch(
        f"/visits/{visit['id']}",
        json={"visit_date": visit["visit_date"], "doctor_id": str(doctor.id), "notes": "Canals located",
              "prescription": {"items": [{"medicine": "Paracetamol 650 mg"}]}},
        headers=auth(other_token),
    )
    assert notes_only.status_code == 200
    assert notes_only.json()["visits"][0]["notes"] == "Canals located"


def test_handing_a_treatment_to_another_doctor_is_kept_as_history(client):
    clinic_id, owner, _, doctor = _setup()
    rct = _service(client, sign_in(client, owner))
    other = make_staff(clinic_id, "smile", "endo@smile.test", "doctor")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id, start=True).json()

    same = client.post(f"/treatments/{t['id']}/handoff", json={"to_doctor_id": str(doctor.id)}, headers=auth(token))
    assert same.status_code == 400
    moved = client.post(
        f"/treatments/{t['id']}/handoff", json={"to_doctor_id": str(other.id), "reason": "Needs an endodontist"}, headers=auth(token)
    ).json()
    assert moved["doctor"]["name"] == "Endo"
    assert [(h["from_doctor"]["name"], h["to_doctor"]["name"], h["reason"]) for h in moved["handoffs"]] == [
        ("Doc", "Endo", "Needs an endodontist")
    ]


def test_recommendations_are_suggested_until_they_become_treatments(client):
    _, owner, _, doctor = _setup()
    owner_token = sign_in(client, owner)
    rct = _service(client, owner_token)
    scaling = _service(client, owner_token, name="Scaling and polishing", price=150000)
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    consultation = client.post(
        f"/patients/{patient['id']}/consultations",
        json={
            "doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Caries on 36",
            "recommendations": [{"service_id": rct["id"], "teeth": [36]}, {"service_id": scaling["id"], "teeth": []}],
        },
        headers=auth(token),
    ).json()

    suggested = client.get(f"/patients/{patient['id']}/treatments/suggestions", headers=auth(token)).json()
    assert sorted((s["service"]["name"], tuple(s["teeth"])) for s in suggested) == [
        ("Root canal treatment", (36,)), ("Scaling and polishing", ()),
    ]
    _treatment(client, token, patient["id"], rct["id"], doctor.id, teeth=[36], consultation_id=consultation["id"])
    left = client.get(f"/patients/{patient['id']}/treatments/suggestions", headers=auth(token)).json()
    assert [s["service"]["name"] for s in left] == ["Scaling and polishing"]


def test_teeth_are_for_standard_and_pro(client):
    _, owner, _, doctor = _setup(plan="basic")
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    assert _treatment(client, token, patient["id"], rct["id"], doctor.id, teeth=[36]).status_code == 403
    assert _treatment(client, token, patient["id"], rct["id"], doctor.id).status_code == 201


def test_the_service_changes_only_while_planned_and_takes_the_new_price(client):
    _, owner, desk, doctor = _setup()
    owner_token = sign_in(client, owner)
    rct = _service(client, owner_token)
    crown = _service(client, owner_token, name="Zirconia crown", price=1200000)
    token = sign_in(client, desk)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()

    changed = client.patch(f"/treatments/{t['id']}", json={"service_id": crown["id"], "teeth": [36]}, headers=auth(token)).json()
    assert (changed["service"]["name"], changed["money"]["price_paise"]) == ("Zirconia crown", 1200000)
    client.post(f"/treatments/{t['id']}/start", json={}, headers=auth(token))
    too_late = client.patch(f"/treatments/{t['id']}", json={"service_id": rct["id"], "teeth": [36]}, headers=auth(token))
    assert too_late.status_code == 409


def test_treatments_are_listed_ongoing_first_and_logged(client):
    clinic_id, owner, _, doctor = _setup()
    owner_token = sign_in(client, owner)
    rct = _service(client, owner_token)
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _treatment(client, token, patient["id"], rct["id"], doctor.id)
    _treatment(client, token, patient["id"], rct["id"], doctor.id, start=True)
    statuses = [t["status"] for t in client.get(f"/patients/{patient['id']}/treatments", headers=auth(token)).json()]
    assert statuses == ["ongoing", "planned"]
    actions = [a for (a,) in _rows(clinic_id, "SELECT action FROM audit_log WHERE entity_type = 'treatment' ORDER BY occurred_at")]
    assert actions == ["create", "create"]


def test_another_clinic_cannot_reach_the_treatments(client):
    _, owner, _, doctor = _setup("smile")
    rct = _service(client, sign_in(client, owner))
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], rct["id"], doctor.id).json()

    _, _, _, stranger = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/treatments", headers=auth(other)).status_code == 404
    assert client.post(f"/treatments/{t['id']}/start", json={}, headers=auth(other)).status_code == 404
    assert _treatment(client, other, patient["id"], rct["id"], stranger.id).status_code == 404


def test_automatic_recalls_come_with_standard_and_pro(client):
    clinic_id = make_clinic("basic-clinic", plan="basic")
    owner = make_staff(clinic_id, "basic-clinic", "owner@basic.test", "owner")
    doctor = make_staff(clinic_id, "basic-clinic", "doc@basic.test", "doctor")
    scaling = _service(client, sign_in(client, owner), name="Scaling and polishing", price=150000, recall=6)
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    t = _treatment(client, token, patient["id"], scaling["id"], doctor.id, start=True).json()
    client.post(f"/treatments/{t['id']}/finish", json={}, headers=auth(token))
    follow_ups = client.get(f"/patients/{patient['id']}/follow-ups", headers=auth(token)).json()
    assert [f for f in follow_ups if f["source"] == "recall"] == []
