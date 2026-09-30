import io
import time
import uuid
from datetime import timedelta

from PIL import Image
from sqlalchemy import text

from app.files_service import ViewGrant, view_token
from app.plans import GB
from tests.conftest import auth, make_clinic, make_staff, owner_engine, sign_in


def _setup(slug="smile", plan="standard"):
    clinic_id = make_clinic(slug, plan=plan)
    doctor = make_staff(clinic_id, slug, f"doc@{slug}.test", "doctor")
    return clinic_id, doctor


def _patient(client, token):
    return client.post(
        "/patients", json={"name": "Rahul Sharma", "phone": "9810012345", "birth_year": 1990}, headers=auth(token)
    ).json()


def _jpeg(width=1600, height=1200) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 40, 40)).save(out, format="JPEG")
    return out.getvalue()


PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def _upload(client, token, patient_id, data, content_type="image/jpeg", **details):
    """The three steps the app takes. Returns the final response."""
    ticket = client.post(
        f"/patients/{patient_id}/files/uploads",
        json={"content_type": content_type, "size_bytes": len(data)},
        headers=auth(token),
    )
    assert ticket.status_code == 200, ticket.text
    body = ticket.json()
    sent = client.put(body["upload_url"], content=data, headers=body["headers"])
    assert sent.status_code == 204, sent.text
    details = {"kind": "xray", **details}
    return client.post(
        f"/patients/{patient_id}/files", json={"upload_token": body["upload_token"], **details}, headers=auth(token)
    )


def _audit(clinic_id, entity_type="file"):
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        return connection.execute(
            text("SELECT action FROM audit_log WHERE entity_type = :t ORDER BY occurred_at"), {"t": entity_type}
        ).scalars().all()


def test_an_xray_is_uploaded_with_a_thumbnail(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)

    saved = _upload(client, token, patient["id"], _jpeg(), title="OPG before root canal", teeth=[36, 37])
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert (body["kind"], body["title"], body["teeth"]) == ("xray", "OPG before root canal", [36, 37])
    assert (body["width"], body["height"]) == (1600, 1200)
    assert body["uploaded_by"]["name"] == "Doc"

    thumbnail = client.get(body["thumbnail_url"])
    assert thumbnail.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(thumbnail.content)).size) == 480

    # The signed link works without the sign in, as an <img> needs.
    original = client.get(body["view_url"])
    assert original.content == _jpeg()

    listed = client.get(f"/patients/{patient['id']}/files", headers=auth(token)).json()
    assert [f["id"] for f in listed] == [body["id"]]
    assert _audit(clinic_id) == ["create"]


def test_a_pdf_is_kept_without_a_thumbnail_and_downloads_with_a_name(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    body = _upload(client, token, patient["id"], PDF, content_type="application/pdf", kind="document", title="Referral letter").json()
    assert body["thumbnail_url"] is None

    download = client.get(body["download_url"])
    assert download.content == PDF
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'{patient["code"]} Referral letter' in disposition
    assert _audit(clinic_id) == ["create", "download"]


def test_only_images_and_pdfs_under_25_mb_are_accepted(client):
    _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    wrong_type = client.post(
        f"/patients/{patient['id']}/files/uploads", json={"content_type": "text/plain", "size_bytes": 10}, headers=auth(token)
    )
    assert wrong_type.status_code == 422
    too_big = client.post(
        f"/patients/{patient['id']}/files/uploads",
        json={"content_type": "image/jpeg", "size_bytes": 26 * 1024 * 1024},
        headers=auth(token),
    )
    assert too_big.status_code == 422
    assert "25 MB" in too_big.json()["detail"]


def test_a_file_that_is_not_what_it_claims_is_refused_and_removed(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    fake_image = _upload(client, token, patient["id"], b"this is not a picture at all")
    assert fake_image.status_code == 422
    fake_pdf = _upload(client, token, patient["id"], b"not a pdf either", content_type="application/pdf", kind="document")
    assert fake_pdf.status_code == 422
    assert client.get(f"/patients/{patient['id']}/files", headers=auth(token)).json() == []


def test_the_upload_link_accepts_only_what_was_announced(client):
    _, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    ticket = client.post(
        f"/patients/{patient['id']}/files/uploads", json={"content_type": "image/jpeg", "size_bytes": 100}, headers=auth(token)
    ).json()

    bigger = client.put(ticket["upload_url"], content=b"x" * 101, headers=ticket["headers"])
    assert bigger.status_code == 413
    other_type = client.put(ticket["upload_url"], content=b"x" * 10, headers={"Content-Type": "application/pdf"})
    assert other_type.status_code == 400
    tampered = client.put(ticket["upload_url"] + "x", content=b"x" * 10, headers=ticket["headers"])
    assert tampered.status_code == 403


def test_an_upload_cannot_be_confirmed_for_another_patient(client):
    _, doctor = _setup()
    token = sign_in(client, doctor)
    first = _patient(client, token)
    second = client.post("/patients", json={"name": "Asha Rao", "phone": "9810099999", "birth_year": 1985}, headers=auth(token)).json()
    ticket = client.post(
        f"/patients/{first['id']}/files/uploads", json={"content_type": "image/jpeg", "size_bytes": len(_jpeg())}, headers=auth(token)
    ).json()
    client.put(ticket["upload_url"], content=_jpeg(), headers=ticket["headers"])
    wrong = client.post(
        f"/patients/{second['id']}/files", json={"upload_token": ticket["upload_token"], "kind": "xray"}, headers=auth(token)
    )
    assert wrong.status_code == 400
    # Nothing arrived yet for a fresh link, so there is nothing to confirm.
    fresh = client.post(
        f"/patients/{first['id']}/files/uploads", json={"content_type": "image/jpeg", "size_bytes": 50}, headers=auth(token)
    ).json()
    missing = client.post(
        f"/patients/{first['id']}/files", json={"upload_token": fresh["upload_token"], "kind": "xray"}, headers=auth(token)
    )
    assert missing.status_code == 400


def test_uploads_stop_when_the_storage_is_full(client):
    clinic_id, doctor = _setup(plan="basic")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    # Pretend the clinic already holds nearly 3 GB, the Basic plan's storage.
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(
            text(
                "INSERT INTO files (id, clinic_id, patient_id, kind, storage_key, content_type, size_bytes, uploaded_by) "
                "VALUES (:id, :cid, :pid, 'xray', 'clinics/x/big.jpg', 'image/jpeg', :size, :by)"
            ),
            {"id": uuid.uuid4(), "cid": clinic_id, "pid": patient["id"], "size": 3 * GB - 1000, "by": doctor.id},
        )
    full = client.post(
        f"/patients/{patient['id']}/files/uploads", json={"content_type": "image/jpeg", "size_bytes": 5000}, headers=auth(token)
    )
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "storage_limit"


def test_teeth_can_be_tagged_on_standard_but_not_on_basic(client):
    _, doctor = _setup(plan="basic")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    tagged = _upload(client, token, patient["id"], _jpeg(), teeth=[36])
    assert tagged.status_code == 403
    untagged = _upload(client, token, patient["id"], _jpeg())
    assert untagged.status_code == 201

    not_a_tooth = client.patch(
        f"/files/{untagged.json()['id']}", json={"kind": "xray", "teeth": [99]}, headers=auth(token)
    )
    assert not_a_tooth.status_code == 422


def test_details_can_be_edited_and_are_logged(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    saved = _upload(client, token, patient["id"], _jpeg()).json()
    edited = client.patch(
        f"/files/{saved['id']}", json={"kind": "photo", "title": "Smile before whitening", "teeth": [11, 21]}, headers=auth(token)
    )
    assert edited.status_code == 200
    assert (edited.json()["kind"], edited.json()["teeth"]) == ("photo", [11, 21])
    assert _audit(clinic_id) == ["create", "update"]


def test_the_bin_keeps_files_for_30_days_and_they_can_be_restored(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    saved = _upload(client, token, patient["id"], _jpeg()).json()

    binned = client.delete(f"/files/{saved['id']}", headers=auth(token)).json()
    assert binned["deleted_at"] is not None
    assert client.get(f"/patients/{patient['id']}/files", headers=auth(token)).json() == []
    in_bin = client.get(f"/patients/{patient['id']}/files?bin=true", headers=auth(token)).json()
    assert [f["id"] for f in in_bin] == [saved["id"]]
    # Still counts towards storage until removed for good.
    assert client.patch(f"/files/{saved['id']}", json={"kind": "photo"}, headers=auth(token)).status_code == 409

    restored = client.post(f"/files/{saved['id']}/restore", headers=auth(token)).json()
    assert restored["deleted_at"] is None
    assert _audit(clinic_id) == ["create", "delete", "restore"]


def test_a_file_past_its_30_days_cannot_be_restored(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    saved = _upload(client, token, patient["id"], _jpeg()).json()
    client.delete(f"/files/{saved['id']}", headers=auth(token))
    with owner_engine.begin() as connection:
        connection.execute(text("select set_config('app.clinic_id', :cid, true)"), {"cid": str(clinic_id)})
        connection.execute(text("UPDATE files SET purge_after = now() - interval '1 minute'"))
    assert client.post(f"/files/{saved['id']}/restore", headers=auth(token)).status_code == 410


def test_viewing_links_expire(client):
    clinic_id, doctor = _setup()
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    saved = _upload(client, token, patient["id"], _jpeg()).json()
    old = view_token(
        ViewGrant(clinic_id, uuid.UUID(saved["id"]), thumbnail=False, download=False),
        now=time.time() - timedelta(hours=2).total_seconds(),
    )
    assert client.get(f"/files/content/{old}").status_code == 403


def test_another_clinic_cannot_see_or_change_the_files(client):
    _, doctor = _setup("smile")
    token = sign_in(client, doctor)
    patient = _patient(client, token)
    saved = _upload(client, token, patient["id"], _jpeg()).json()

    _, stranger = _setup("bright")
    other = sign_in(client, stranger)
    assert client.get(f"/patients/{patient['id']}/files", headers=auth(other)).status_code == 404
    assert client.delete(f"/files/{saved['id']}", headers=auth(other)).status_code == 404
    assert client.patch(f"/files/{saved['id']}", json={"kind": "photo"}, headers=auth(other)).status_code == 404


def test_the_plan_page_counts_uploaded_files(client):
    clinic_id = make_clinic("smile", plan="standard")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")
    token = sign_in(client, owner)
    patient = _patient(client, token)
    _upload(client, token, patient["id"], _jpeg())
    used = client.get("/plan", headers=auth(token)).json()["usage"]["storage_bytes"]["used"]
    assert used == len(_jpeg())
