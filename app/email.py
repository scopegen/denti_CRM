import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("denti.email")


class EmailNotSent(Exception):
    """The mail server could not be reached or refused the email. The API answers 503."""


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
    if settings.email_backend == "smtp":
        _send_smtp(message)
        return
    raise NotImplementedError(f"Email backend {settings.email_backend!r} is not set up yet")


def _send_smtp(message: Email) -> None:
    """Through a mail account's server, such as one made in cPanel. Port 465 is encrypted from
    the start; any other port (587) switches to encryption before signing in."""
    mail = EmailMessage()
    mail["From"] = settings.email_from
    mail["To"] = message.to
    mail["Subject"] = message.subject
    mail.set_content(message.body)
    context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            server.starttls(context=context)
        with server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(mail)
    except (smtplib.SMTPException, OSError) as error:
        logger.error("Could not send email %r to %s: %s", message.subject, message.to, error)
        raise EmailNotSent from error


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


def send_signup_code(to: str, clinic_name: str, code: str) -> None:
    send_email(
        to,
        f"Your Denti sign up code: {code}",
        f"Your code to finish setting up {clinic_name} on Denti is {code}.\n"
        f"It expires in {settings.login_code_minutes} minutes.\n\n"
        "If you did not sign up for Denti, you can ignore this email.",
    )
