import base64
import io
import uuid

from openpyxl import Workbook
from sqlalchemy import text

from tests.conftest import add_picklist, auth, make_clinic, make_staff, owner_engine, sign_in

CSV = """Patient ID,Patient Name,Mobile No,Sex,DOB,Age,City,Medical Conditions,Outstanding
RANCO-0012,Asha Rao,+91 10100 00001,F,12/03/1985,,Noida,Diabetes,"1,200"
RANCO-0015,Vikram Singh,01010000002,Male,,42,Delhi,"Diabetes, Migraine",0
RANCO-0016,,1010000003,M,,30,Noida,,
RANCO-0017,Meera Iyer,12345,F,,25,Noida,,
RANCO-0018,Asha Rao,1010000001,F,,40,Noida,,
RANCO-0019,Ravi Kumar,1010000009,M,not a date,,Noida,,abc
"""


def _setup(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    add_picklist(clinic_id, "medical_condition", ["Diabetes", "Hypertension"])
    owner = make_staff(clinic_id, slug, f"owner@{slug}.test", "owner")
    desk = make_staff(clinic_id, slug, f"desk@{slug}.test", "admin")
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, owner, desk, doctor


def _upload(client, token, content: bytes, name="patients.csv"):
    body = {"file_name": name, "data_base64": base64.b64encode(content).decode()}
    return client.post("/imports/files", json=body, headers=auth(token))


def _sql(clinic_id, sql, **params):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(text(sql), params).all()


def test_the_columns_are_matched_from_their_headings(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode())
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["row_count"] == 6
    mapping = body["mapping"]
    columns = body["columns"]
    assert {key: columns[i] for key, i in mapping.items() if i is not None} == {
        "name": "Patient Name", "phone": "Mobile No", "previous_id": "Patient ID", "gender": "Sex", "dob": "DOB",
        "age": "Age", "city": "City", "medical_conditions": "Medical Conditions", "opening_balance": "Outstanding",
    }
    assert body["sample"][0][1] == "Asha Rao"


def test_an_excel_file_is_read_too(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    book = Workbook()
    sheet = book.active
    sheet.append(["Name", "Phone", "Date of birth"])
    sheet.append(["Asha Rao", 1010000001, None])
    out = io.BytesIO()
    book.save(out)
    preview = _upload(client, token, out.getvalue(), name="patients.xlsx").json()
    assert preview["row_count"] == 1
    assert preview["sample"][0][:2] == ["Asha Rao", "1010000001"]


def test_wrong_files_are_refused(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    assert _upload(client, token, b"hello", name="patients.pdf").status_code == 422
    assert _upload(client, token, b"Name,Phone\n", name="empty.csv").status_code == 422


def test_every_problem_row_is_flagged_before_saving(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    checked = client.post("/imports/check", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token))
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert (body["ok"], body["duplicates"], body["invalid"]) == (2, 1, 3)
    flagged = {r["row"]: (r["kind"], r["problems"], r["warnings"]) for r in body["flagged"]}
    assert flagged[4] == ("invalid", ["Name is missing"], [])
    assert flagged[5] == ("invalid", ["Phone number looks wrong"], [])
    assert flagged[6] == ("duplicate", ["Same patient as row 2"], [])
    assert flagged[7][0] == "invalid"
    assert "Balance owed is not an amount" in flagged[7][1]
    assert "Date of birth not understood, left empty" in flagged[7][2]
    assert body["plan"]["fits"] is True
    assert body["keep_numbers_possible"] is True


def test_importing_creates_patients_with_their_old_ids_and_balances(client):
    clinic_id, _, desk, _ = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    run = client.post(
        "/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"], "include_rows": [6]}, headers=auth(token)
    )
    assert run.status_code == 201, run.text
    batch = run.json()
    assert (batch["rows_total"], batch["rows_imported"], batch["rows_skipped"], batch["can_undo"]) == (6, 3, 3, True)

    patients = _sql(clinic_id, "SELECT patient_number, name, phone, previous_id, gender, birth_year, medical_conditions, medical_history FROM patients ORDER BY patient_number")
    assert [(p[0], p[1], p[2], p[3], p[4]) for p in patients] == [
        (1, "Asha Rao", "1010000001", "RANCO-0012", "female"),
        (2, "Vikram Singh", "1010000002", "RANCO-0015", "male"),
        (3, "Asha Rao", "1010000001", "RANCO-0018", "female"),
    ]
    assert patients[1][6] == ["Diabetes"] and "Other conditions: Migraine" in patients[1][7]
    balances = _sql(clinic_id, "SELECT amount_paise, kind FROM ledger_entries")
    assert balances == [(120000, "opening_balance")]
    # The old software's IDs are searchable.
    found = client.get("/patients", params={"q": "ranco-0015"}, headers=auth(token)).json()
    assert [p["name"] for p in found["items"]] == ["Vikram Singh"]


def test_existing_patient_numbers_can_be_kept(client):
    clinic_id, _, desk, _ = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"], "keep_numbers": True}, headers=auth(token))
    numbers = [n for (n,) in _sql(clinic_id, "SELECT patient_number FROM patients ORDER BY 1")]
    assert numbers == [12, 15]
    # The next patient registered continues from the highest kept number.
    new = client.post("/patients", json={"name": "New Patient", "phone": "1010000555", "birth_year": 1990}, headers=auth(token)).json()
    assert new["patient_number"] == 16


def test_numbers_are_not_kept_when_they_clash(client):
    _, _, desk, _ = _setup()
    token = sign_in(client, desk)
    for i in range(15):
        client.post("/patients", json={"name": f"P {i}", "phone": f"98200000{i:02d}", "birth_year": 1990}, headers=auth(token))
    preview = _upload(client, token, CSV.encode()).json()
    checked = client.post("/imports/check", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token)).json()
    assert checked["keep_numbers_possible"] is False
    assert "already used" in checked["keep_numbers_reason"]
    refused = client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"], "keep_numbers": True}, headers=auth(token))
    assert refused.status_code == 409


def test_an_import_that_does_not_fit_the_plan_suggests_one_that_does(client):
    clinic_id, _, desk, _ = _setup(plan="basic")
    token = sign_in(client, desk)
    _sql(
        clinic_id,
        "INSERT INTO patients (id, clinic_id, patient_number, name, phone, registered_by, medical_conditions, birth_year) "
        "SELECT gen_random_uuid(), :cid, n, 'Patient ' || n, '97' || lpad(n::text, 8, '0'), :by, '{}', 1990 FROM generate_series(1000, 1499) n "
        "RETURNING 1",
        cid=clinic_id, by=desk.id,
    )
    preview = _upload(client, token, CSV.encode()).json()
    checked = client.post("/imports/check", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token)).json()
    assert checked["plan"] == {
        "used": 500, "adding": 2, "limit": 500, "fits": False, "suggested_plan": "standard", "suggested_plan_name": "Standard",
    }
    refused = client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token))
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "plan_limit"


def test_an_import_can_be_undone_within_30_minutes(client):
    clinic_id, _, desk, _ = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    batch = client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token)).json()

    undone = client.post(f"/imports/{batch['id']}/undo", headers=auth(token))
    assert undone.status_code == 200, undone.text
    assert undone.json()["undone_at"] is not None and undone.json()["can_undo"] is False
    assert _sql(clinic_id, "SELECT count(*) FROM patients") == [(0,)]
    assert _sql(clinic_id, "SELECT count(*) FROM ledger_entries") == [(0,)]
    # The history of the import and its undo stays.
    actions = [a for (a,) in _sql(clinic_id, "SELECT action FROM audit_log WHERE entity_type = 'import_batch' ORDER BY occurred_at")]
    assert actions == ["import", "undo"]
    assert client.post(f"/imports/{batch['id']}/undo", headers=auth(token)).status_code == 409


def test_an_import_cannot_be_undone_once_something_is_recorded_or_time_is_up(client):
    clinic_id, _, desk, doctor = _setup()
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    batch = client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token)).json()
    patient_id = _sql(clinic_id, "SELECT id FROM patients ORDER BY patient_number LIMIT 1")[0][0]
    client.post("/arrivals", json={"patient_id": str(patient_id)}, headers=auth(token))
    recorded = client.post(f"/imports/{batch['id']}/undo", headers=auth(token))
    assert recorded.status_code == 409
    assert "already been recorded" in recorded.json()["detail"]

    preview2 = _upload(client, token, "Name,Phone\nLate Patient,1010000777\n".encode()).json()
    batch2 = client.post("/imports", json={"file_id": preview2["file_id"], "mapping": preview2["mapping"]}, headers=auth(token)).json()
    _sql(clinic_id, "UPDATE import_batches SET undo_deadline = now() - interval '1 minute' WHERE id = :id RETURNING 1", id=batch2["id"])
    late = client.post(f"/imports/{batch2['id']}/undo", headers=auth(token))
    assert late.status_code == 409
    assert "30 minutes" in late.json()["detail"]


def test_doctors_cannot_import(client):
    _, _, _, doctor = _setup()
    token = sign_in(client, doctor)
    assert _upload(client, token, CSV.encode()).status_code == 403


def test_another_clinic_cannot_use_the_file_or_undo_the_import(client):
    _, _, desk, _ = _setup("smile")
    token = sign_in(client, desk)
    preview = _upload(client, token, CSV.encode()).json()
    batch = client.post("/imports", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(token)).json()

    _, _, stranger, _ = _setup("bright")
    other = sign_in(client, stranger)
    assert client.post("/imports/check", json={"file_id": preview["file_id"], "mapping": preview["mapping"]}, headers=auth(other)).status_code == 404
    assert client.post(f"/imports/{batch['id']}/undo", headers=auth(other)).status_code == 404
    assert client.post(f"/imports/{uuid.uuid4()}/undo", headers=auth(token)).status_code == 404
