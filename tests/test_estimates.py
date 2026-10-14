from datetime import date, timedelta

from sqlalchemy import text

from app.routers.invoices import financial_year
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _services(client, token):
    out = {}
    for name, price in (("Root canal treatment", 650000), ("Ceramic crown", 900000), ("Extraction", 120000), ("Dental implant", 3200000)):
        out[name] = client.post("/services", json={"name": name, "kind": "in_house", "listed_price_paise": price}, headers=auth(token)).json()
    return out


def _patient(client, token):
    return client.post("/patients", json={"name": "Rohan Das", "phone": "1060012345", "birth_year": 1980}, headers=auth(token)).json()


def _two_options(services, doctor_id, **extra):
    """Save the tooth (root canal and crown) or replace it (extraction and implant)."""
    return {
        "doctor_id": str(doctor_id),
        "notes": "Crown fitted two weeks after the root canal.",
        "options": [
            {"label": "Option A: save the tooth", "items": [
                {"service_id": services["Root canal treatment"]["id"], "teeth": [36], "sittings": 3, "unit_price_paise": 650000},
                {"service_id": services["Ceramic crown"]["id"], "teeth": [36], "sittings": 2, "unit_price_paise": 900000},
            ]},
            {"label": "Option B: replace the tooth", "items": [
                {"service_id": services["Extraction"]["id"], "teeth": [36], "sittings": 1, "unit_price_paise": 120000},
                {"service_id": services["Dental implant"]["id"], "teeth": [36], "sittings": 3, "unit_price_paise": 3200000},
            ]},
        ],
        **extra,
    }


def test_an_estimate_is_numbered_valid_for_30_days_and_totals_each_option(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)

    created = client.post(
        f"/patients/{patient['id']}/estimates",
        json=_two_options(services, doctor.id, discount_type="percent", discount_value=1000),
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    estimate = created.json()[0]
    assert estimate["number_text"] == f"EST/{financial_year(date.today())}/0001"
    assert estimate["valid_until"] == (date.today() + timedelta(days=30)).isoformat()
    assert estimate["status"] == "given"
    totals = [(o["label"], o["subtotal_paise"], o["discount_paise"], o["total_paise"]) for o in estimate["options"]]
    assert totals == [
        ("Option A: save the tooth", 1550000, 155000, 1395000),
        ("Option B: replace the tooth", 3320000, 332000, 2988000),
    ]
    assert estimate["options"][0]["items"][0]["description"] == "Root canal treatment"


def test_accepting_an_option_plans_its_treatments_at_the_quoted_prices(client):
    clinic_id, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    # ₹999 off ₹15,500: it cannot split evenly, so the last item takes the rounding.
    estimate = client.post(
        f"/patients/{patient['id']}/estimates",
        json=_two_options(services, doctor.id, discount_type="amount", discount_value=99900),
        headers=auth(token),
    ).json()[0]
    option_a = estimate["options"][0]

    accepted = client.post(f"/estimates/{estimate['id']}/accept", json={"option_id": option_a["id"]}, headers=auth(token)).json()[0]
    assert (accepted["status"], accepted["accepted_option_id"]) == ("accepted", option_a["id"])

    treatments = client.get(f"/patients/{patient['id']}/treatments", headers=auth(token)).json()
    planned = sorted((t["service"]["name"], t["status"], t["teeth"], t["money"]["final_price_paise"]) for t in treatments)
    assert planned == [("Ceramic crown", "planned", [36], 841993), ("Root canal treatment", "planned", [36], 608107)]
    assert sum(p[3] for p in planned) == option_a["total_paise"]
    # Planned treatments are not billed until they start.
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        assert connection.execute(text("SELECT count(*) FROM ledger_entries")).scalar() == 0

    again = client.post(f"/estimates/{estimate['id']}/accept", json={"option_id": option_a["id"]}, headers=auth(token))
    assert again.status_code == 409


def test_an_option_must_belong_to_the_estimate(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    first = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).json()[0]
    second = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).json()[0]
    assert second["number_text"].endswith("0002")
    wrong = client.post(f"/estimates/{first['id']}/accept", json={"option_id": second["options"][0]["id"]}, headers=auth(token))
    assert wrong.status_code == 400


def test_a_declined_estimate_stays_on_record_and_an_old_one_shows_expired(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    declined = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).json()[0]
    after = client.post(f"/estimates/{declined['id']}/decline", headers=auth(token)).json()
    assert after[0]["status"] == "declined"

    today = date.today().isoformat()
    short = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id, valid_until=today), headers=auth(token))
    assert short.json()[0]["status"] == "given"
    past = client.post(
        f"/patients/{patient['id']}/estimates",
        json=_two_options(services, doctor.id, valid_until=(date.today() - timedelta(days=1)).isoformat()),
        headers=auth(token),
    )
    assert past.status_code == 422


def test_doctors_see_estimates_but_never_the_amounts(client):
    _, _, desk, doctor = _setup()
    desk_token = sign_in(client, desk)
    services = _services(client, desk_token)
    patient = _patient(client, desk_token)
    client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(desk_token))

    token = sign_in(client, doctor)
    seen = client.get(f"/patients/{patient['id']}/estimates", headers=auth(token)).json()[0]
    assert seen["status"] == "given"
    assert seen["notes"] is None and seen["discount_value"] is None
    for option in seen["options"]:
        assert option["total_paise"] is None
        assert all(i["unit_price_paise"] is None and i["amount_paise"] is None for i in option["items"])
    assert client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).status_code == 403
    assert client.get(f"/estimates/{seen['id']}/pdf", headers=auth(token)).status_code == 403


def test_teeth_on_an_estimate_are_for_standard_and_pro(client):
    _, _, desk, doctor = _setup(plan="basic")
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    assert client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).status_code == 403


def test_the_estimate_prints_as_a_pdf(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    estimate = client.post(
        f"/patients/{patient['id']}/estimates",
        json=_two_options(services, doctor.id, discount_type="percent", discount_value=500),
        headers=auth(token),
    ).json()[0]
    pdf = client.get(f"/estimates/{estimate['id']}/pdf?download=true", headers=auth(token))
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert "Estimate EST-" in pdf.headers["content-disposition"]


def test_another_clinic_cannot_see_the_estimates(client):
    _, _, desk, doctor = _setup("smile")
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    estimate = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).json()[0]

    _, _, stranger, _ = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/estimates", headers=auth(other)).status_code == 404
    assert client.post(f"/estimates/{estimate['id']}/decline", headers=auth(other)).status_code == 404


def test_an_accepted_estimate_takes_the_recommendation_off_the_list(client):
    _, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    services = _services(client, token)
    patient = _patient(client, token)
    client.post(
        f"/patients/{patient['id']}/consultations",
        json={"doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Fractured 36",
              "recommendations": [{"service_id": services["Root canal treatment"]["id"], "teeth": [36]}]},
        headers=auth(token),
    )
    assert len(client.get(f"/patients/{patient['id']}/treatments/suggestions", headers=auth(token)).json()) == 1
    estimate = client.post(f"/patients/{patient['id']}/estimates", json=_two_options(services, doctor.id), headers=auth(token)).json()[0]
    client.post(f"/estimates/{estimate['id']}/accept", json={"option_id": estimate["options"][0]["id"]}, headers=auth(token))
    assert client.get(f"/patients/{patient['id']}/treatments/suggestions", headers=auth(token)).json() == []
