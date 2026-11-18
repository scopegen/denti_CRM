import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {"database_url": "postgresql+psycopg://x@localhost/x", "migration_database_url": "postgresql+psycopg://x@localhost/x", "jwt_secret": "x"}


def test_a_live_server_will_not_start_unless_it_can_email_codes():
    with pytest.raises(ValidationError, match="EMAIL_BACKEND is console"):
        Settings(_env_file=None, environment="production", email_backend="console", **REQUIRED)
    with pytest.raises(ValidationError, match="SMTP_HOST, SMTP_USER or SMTP_PASSWORD is missing"):
        Settings(_env_file=None, environment="production", email_backend="smtp", smtp_host="mail.example.test", **REQUIRED)
    ready = Settings(
        _env_file=None, environment="production", email_backend="smtp", smtp_host="mail.example.test",
        smtp_user="no-reply@example.test", smtp_password="secret", **REQUIRED,
    )
    assert ready.email_backend == "smtp"
    # Local development keeps printing codes in the backend window.
    assert Settings(_env_file=None, environment="development", **REQUIRED).email_backend == "console"
