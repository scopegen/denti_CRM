import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings

_JWT_ALGORITHM = "HS256"


def now() -> datetime:
    return datetime.now(timezone.utc)


# Passwords

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# A real hash to compare against when the email is unknown, so a wrong email takes as
# long as a wrong password and response times do not reveal which accounts exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def burn_password_check(plain: str) -> None:
    verify_password(plain, _DUMMY_HASH)


# Emailed codes and random tokens

def new_login_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_login_code(code_id: uuid.UUID, code: str) -> str:
    key = f"{settings.jwt_secret}:login-code".encode("utf-8")
    return hmac.new(key, f"{code_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Access tokens

@dataclass(frozen=True)
class AccessClaims:
    staff_id: uuid.UUID
    clinic_id: uuid.UUID
    role: str
    token_version: int


def create_access_token(claims: AccessClaims) -> str:
    payload = {
        "sub": str(claims.staff_id),
        "cid": str(claims.clinic_id),
        "role": claims.role,
        "tv": claims.token_version,
        "exp": now() + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> AccessClaims | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
        return AccessClaims(
            staff_id=uuid.UUID(payload["sub"]),
            clinic_id=uuid.UUID(payload["cid"]),
            role=str(payload["role"]),
            token_version=int(payload["tv"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def mask_email(email: str) -> str:
    """o****@smiledental.in, for "we sent a code to ..." messages."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}****@{domain}" if domain else "your email"
