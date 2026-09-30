import re
from datetime import date

from app.models import Clinic

_CODE = re.compile(r"^(?:[A-Za-z0-9]+-)?0*(\d{1,9})$")


def patient_prefix(clinic: Clinic) -> str:
    """The clinic's chosen prefix, or one made from its web address (smile-dental gives SMILE)."""
    if clinic.patient_id_prefix:
        return clinic.patient_id_prefix
    letters = re.sub(r"[^a-z0-9]", "", clinic.slug.split("-")[0])
    return (letters[:6] or "PT").upper()


def patient_code(clinic: Clinic, number: int) -> str:
    """SMILE-0042. Printed on documents patients keep, and used in the web address."""
    return f"{patient_prefix(clinic)}-{number:04d}"


def parse_patient_number(value: str) -> int | None:
    """Reads SMILE-0042, smile-42, 0042 or 42 as patient number 42."""
    match = _CODE.match(value.strip())
    return int(match.group(1)) if match else None


def normalise_phone(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def age_on(dob: date | None, birth_year: int | None, day: date) -> int | None:
    """Age in whole years on a given day, from a date of birth or, failing that, a birth year."""
    if dob is not None:
        age = day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))
        return age if age >= 0 else None
    if birth_year is not None:
        age = day.year - birth_year
        return age if age >= 0 else None
    return None
