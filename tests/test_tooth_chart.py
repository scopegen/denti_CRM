from datetime import date

from tests.conftest import add_picklist, auth, make_clinic, make_staff, sign_in

PROBLEMS = ["Cavity", "Broken", "Needs root canal"]


def _setup(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    add_picklist(clinic_id, "tooth_problem", PROBLEMS)
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, doctor


def _patient(client, token):
    return client.post("/patients", json={"name": "Rahul Sharma", "phone": "1010012345", "birth_year": 1990}, headers=auth(token)).json()


def _service(client, owner_token, name, removes_tooth=False, price=100000):
    body = {"name": name, "kind": "in_house", "listed_price_paise": price, "removes_tooth": removes_tooth}
    response = client.post("/services", json=body, headers=auth(owner_token))
    assert response.status_code == 201, response.text
    return response.json()


def _mark(client, token, patient_id, tooth, problem):
    return client.post(f"/patients/{patient_id}/findings", json={"tooth": tooth, "problem": problem}, headers=auth(token))


def _chart(client, token, patient_id):
    response = client.get(f"/patients/{patient_id}/tooth-chart", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def _statuses(chart):
    return {int(tooth): colour for tooth, colour in chart["statuses"].items()}


def _treat(client, token, patient_id, service_id, doctor_id, teeth, finish=False):
    t = client.post(
        f"/patients/{patient_id}/treatments",
        json={"service_id": service_id, "doctor_id": str(doctor_id), "teeth": teeth, "start": finish},
        headers=auth(token),
    ).json()
    if finish:
        t = client.post(f"/treatments/{t['id']}/finish", json={}, headers=auth(token)).json()
    return t


def test_the_chart_is_for_standard_and_pro(client):
    _, _, doctor = _setup(plan="basic")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    assert client.get(f"/patients/{patient['id']}/tooth-chart", headers=auth(token)).status_code == 403
    assert _mark(client, token, patient["id"], 36, "Cavity").status_code == 403
    with_findings = client.post(
        f"/patients/{patient['id']}/consultations",
        json={"doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Caries",
              "findings": [{"tooth": 36, "problem": "Cavity"}]},
        headers=auth(token),
    )
    assert with_findings.status_code == 403


def test_problems_marked_at_a_consultation_show_red(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    consultation = client.post(
        f"/patients/{patient['id']}/consultations",
        json={"doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Caries on 36 and 37",
              "findings": [{"tooth": 36, "problem": "Cavity"}, {"tooth": 37, "problem": "Cavity"}, {"tooth": 46, "problem": "missing"}]},
        headers=auth(token),
    )
    assert consultation.status_code == 201, consultation.text
    assert [(f["tooth"], f["problem"]) for f in consultation.json()["findings"]] == [(36, "Cavity"), (37, "Cavity"), (46, "missing")]

    chart = _chart(client, token, patient["id"])
    assert _statuses(chart) == {36: "problem", 37: "problem", 46: "missing"}
    assert chart["numbering"] == "fdi"
    assert chart["problems"] == PROBLEMS
    assert {f["noted_by"]["name"] for f in chart["findings"]} == {"Doc"}


def test_a_treatment_turns_the_tooth_amber_then_green_when_finished(client):
    _, owner, doctor = _setup()
    rct = _service(client, sign_in(client, owner), "Root canal treatment")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _mark(client, token, patient["id"], 36, "Needs root canal")

    t = _treat(client, token, patient["id"], rct["id"], doctor.id, [36])
    assert _statuses(_chart(client, token, patient["id"]))[36] == "planned"

    client.post(f"/treatments/{t['id']}/start", json={}, headers=auth(token))
    assert _statuses(_chart(client, token, patient["id"]))[36] == "planned"

    client.post(f"/treatments/{t['id']}/finish", json={}, headers=auth(token))
    chart = _chart(client, token, patient["id"])
    assert _statuses(chart)[36] == "done"
    finding = chart["findings"][0]
    assert (finding["resolution"], finding["resolved_by_treatment_id"]) == ("treated", t["id"])

    # A new problem on a treated tooth shows red again.
    _mark(client, token, patient["id"], 36, "Broken")
    assert _statuses(_chart(client, token, patient["id"]))[36] == "problem"


def test_a_cancelled_treatment_leaves_the_problem_red(client):
    _, owner, doctor = _setup()
    rct = _service(client, sign_in(client, owner), "Root canal treatment")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _mark(client, token, patient["id"], 36, "Cavity")
    t = _treat(client, token, patient["id"], rct["id"], doctor.id, [36])
    client.post(f"/treatments/{t['id']}/cancel", json={"reason": "Patient chose extraction"}, headers=auth(token))
    assert _statuses(_chart(client, token, patient["id"])) == {36: "problem"}


def test_an_extraction_leaves_the_tooth_missing_and_an_implant_replaces_it(client):
    _, owner, doctor = _setup()
    owner_token = sign_in(client, owner)
    extraction = _service(client, owner_token, "Extraction", removes_tooth=True)
    implant = _service(client, owner_token, "Dental implant")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _mark(client, token, patient["id"], 46, "Broken")

    _treat(client, token, patient["id"], extraction["id"], doctor.id, [46], finish=True)
    chart = _chart(client, token, patient["id"])
    assert _statuses(chart) == {46: "missing"}
    assert {(f["problem"], f["resolution"]) for f in chart["findings"]} == {("Broken", "treated"), ("missing", None)}

    _treat(client, token, patient["id"], implant["id"], doctor.id, [46], finish=True)
    assert _statuses(_chart(client, token, patient["id"])) == {46: "done"}


def test_a_missing_tooth_has_no_other_problems(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _mark(client, token, patient["id"], 18, "Cavity")
    chart = _mark(client, token, patient["id"], 18, "missing").json()
    assert _statuses(chart) == {18: "missing"}
    assert {(f["problem"], f["resolution"]) for f in chart["findings"]} == {("Cavity", "cleared"), ("missing", None)}

    refused = _mark(client, token, patient["id"], 18, "Broken")
    assert refused.status_code == 409

    missing = next(f for f in chart["findings"] if f["problem"] == "missing")
    present = client.post(f"/findings/{missing['id']}/clear", headers=auth(token)).json()
    assert _statuses(present) == {}
    # Cleared, not deleted: the history keeps it.
    assert len(present["findings"]) == 2


def test_marking_the_same_problem_twice_keeps_one(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    _mark(client, token, patient["id"], 36, "Cavity")
    chart = _mark(client, token, patient["id"], 36, "Cavity").json()
    assert len(chart["findings"]) == 1


def test_teeth_and_problems_are_checked(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    assert _mark(client, token, patient["id"], 19, "Cavity").status_code == 422
    assert _mark(client, token, patient["id"], 36, "Something made up").status_code == 400
    # Milk teeth are on the chart too.
    assert _mark(client, token, patient["id"], 75, "Cavity").status_code == 201


def test_editing_a_consultation_updates_its_problems(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    body = {
        "doctor_id": str(doctor.id), "chief_complaint": "Pain", "oral_examination": "Caries",
        "findings": [{"tooth": 36, "problem": "Cavity"}, {"tooth": 37, "problem": "Cavity"}],
    }
    consultation = client.post(f"/patients/{patient['id']}/consultations", json=body, headers=auth(token)).json()

    body["findings"] = [{"tooth": 36, "problem": "Cavity"}, {"tooth": 11, "problem": "Broken"}]
    edited = client.patch(f"/consultations/{consultation['id']}", json=body, headers=auth(token)).json()
    assert {(f["tooth"], f["problem"], f["resolution"]) for f in edited["findings"]} == {
        (36, "Cavity", None), (37, "Cavity", "cleared"), (11, "Broken", None),
    }
    assert _statuses(_chart(client, token, patient["id"])) == {36: "problem", 11: "problem"}


def test_a_full_mouth_check_is_recorded(client):
    _, _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    assert _chart(client, token, patient["id"])["last_mouth_check"] is None
    chart = client.post(f"/patients/{patient['id']}/mouth-checks", headers=auth(token)).json()
    assert chart["last_mouth_check"]["checked_on"] == date.today().isoformat()
    assert chart["last_mouth_check"]["checked_by"]["name"] == "Doc"


def test_services_remember_whether_they_remove_a_tooth(client):
    _, owner, doctor = _setup()
    owner_token = sign_in(client, owner)
    _service(client, owner_token, "Extraction", removes_tooth=True)
    services = client.get("/services", headers=auth(sign_in(client, doctor))).json()
    assert [(s["name"], s["removes_tooth"]) for s in services] == [("Extraction", True)]


def test_another_clinic_cannot_see_or_change_the_chart(client):
    _, _, doctor = _setup("smile")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    finding = _mark(client, token, patient["id"], 36, "Cavity").json()["findings"][0]

    _, _, stranger = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/tooth-chart", headers=auth(other)).status_code == 404
    assert client.post(f"/findings/{finding['id']}/clear", headers=auth(other)).status_code == 404
    assert _mark(client, other, patient["id"], 36, "Cavity").status_code == 404
