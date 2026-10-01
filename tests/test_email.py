import smtplib

from app import email
from app.config import settings
from tests.conftest import PASSWORD, make_clinic, make_staff


class FakeServer:
    """Stands in for a mail server: records what was sent, or fails like a server that is down."""

    sent: list = []
    fail = False

    def __init__(self, host, port, **kwargs):
        if FakeServer.fail:
            raise OSError("Connection refused")
        self.host, self.port, self.logged_in = host, port, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, **kwargs):
        self.tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, mail):
        FakeServer.sent.append((self.host, self.port, self.logged_in, mail))


def _use_smtp(monkeypatch, port=465):
    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "email_from", "Smile Dental <no-reply@smile.test>")
    monkeypatch.setattr(settings, "smtp_host", "mail.smile.test")
    monkeypatch.setattr(settings, "smtp_port", port)
    monkeypatch.setattr(settings, "smtp_user", "no-reply@smile.test")
    monkeypatch.setattr(settings, "smtp_password", "secret-mail-password")
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeServer)
    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    FakeServer.sent, FakeServer.fail = [], False


def test_sign_in_codes_go_out_through_the_mail_account(client, monkeypatch):
    _use_smtp(monkeypatch)
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    response = client.post("/auth/login", json={"clinic": "smile", "email": owner.email, "password": PASSWORD})
    assert response.status_code == 200
    host, port, logged_in, mail = FakeServer.sent[-1]
    assert (host, port, logged_in) == ("mail.smile.test", 465, ("no-reply@smile.test", "secret-mail-password"))
    assert mail["To"] == "owner@smile.test"
    assert mail["From"] == "Smile Dental <no-reply@smile.test>"
    assert mail["Subject"].startswith("Your Denti sign in code:")
    assert email.outbox == []


def test_port_587_switches_to_encryption_before_signing_in(monkeypatch):
    _use_smtp(monkeypatch, port=587)
    email.send_email("someone@smile.test", "Hello", "Body")
    assert FakeServer.sent[-1][1] == 587


def test_a_mail_server_that_is_down_gives_a_clear_answer(client, monkeypatch):
    _use_smtp(monkeypatch)
    FakeServer.fail = True
    clinic_id = make_clinic("smile")
    owner = make_staff(clinic_id, "smile", "owner@smile.test", "owner")

    response = client.post("/auth/login", json={"clinic": "smile", "email": owner.email, "password": PASSWORD})
    assert response.status_code == 503
    assert response.json()["detail"] == "We could not send the email just now. Please try again in a minute."
