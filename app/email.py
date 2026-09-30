import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("denti.email")


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    body: str


# Every email sent with the console backend, newest last. Tests read codes from here.
outbox: list[Email] = []


def send_email(to: str, subject: str, body: str) -> None:
    message = Email(to=to, subject=subject, body=body)
    if settings.email_backend == "console":
        outbox.append(message)
        # print as well as log, so the code is always visible in the backend window.
        print(f"\n[email to {to}] {subject}\n{body}\n", flush=True)
        return
    # Amazon SES is added before the first real clinic signs up.
    raise NotImplementedError(f"Email backend {settings.email_backend!r} is not set up yet")


def send_login_code(to: str, clinic_name: str, code: str) -> None:
    send_email(
        to,
        f"Your Denti sign in code: {code}",
        f"Your sign in code for {clinic_name} is {code}.\n"
        f"It expires in {settings.login_code_minutes} minutes.\n\n"
        "If you did not try to sign in, you can ignore this email.",
    )


def send_password_reset_code(to: str, clinic_name: str, code: str) -> None:
    send_email(
        to,
        f"Your Denti password reset code: {code}",
        f"Your code to set a new password for {clinic_name} is {code}.\n"
        f"It expires in {settings.login_code_minutes} minutes.\n\n"
        "If you did not ask to reset your password, you can ignore this email. Your password stays the same.",
    )


def send_password_changed(to: str, clinic_name: str) -> None:
    send_email(
        to,
        "Your Denti password was changed",
        f"The password for your {clinic_name} login on Denti was just changed.\n\n"
        "If this was not you, reset your password from the sign in page straight away and tell the clinic owner.",
    )
