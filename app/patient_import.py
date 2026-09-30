"""Reading and checking a patient list from Excel or CSV, before anything is saved.

The file's columns are matched to Denti's fields (suggested from their headings, confirmed by
the person importing). Each row is then checked: rows missing a name or a phone number, or with
a phone number that cannot be right, are not imported; rows that look like a patient already in
the file or already in Denti are skipped unless the person chooses to import them anyway.
Families often share one phone, so the same phone with a different name is not a duplicate.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

MAX_ROWS = 5000

# Denti's fields, in the order the matching screen lists them, with the headings each is
# usually written as in other software and in spreadsheets.
FIELDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("name", "Name", ("name", "patient name", "full name", "patient")),
    ("phone", "Phone", ("phone", "mobile", "mobile number", "mobile no", "phone number", "phone no", "contact", "contact number", "contact no", "whatsapp")),
    ("previous_id", "Old patient ID", ("id", "patient id", "patient no", "patient number", "old id", "registration no", "reg no", "uhid", "file no", "case no")),
    ("gender", "Gender", ("gender", "sex")),
    ("dob", "Date of birth", ("dob", "date of birth", "birth date", "birthdate")),
    ("age", "Age", ("age", "age years")),
    ("email", "Email", ("email", "e mail", "email address", "email id")),
    ("city", "City", ("city", "town")),
    ("area", "Area or address", ("area", "address", "locality", "location")),
    ("medical_conditions", "Medical conditions", ("medical conditions", "medical condition", "conditions", "diseases", "illness")),
    ("medical_history", "Medical history", ("medical history", "history", "notes", "remarks", "comments")),
    ("opening_balance", "Balance owed (₹)", ("balance", "outstanding", "due", "dues", "opening balance", "amount due", "pending", "pending amount", "balance due")),
]
FIELD_KEYS = [key for key, _, _ in FIELDS]


class ImportFileError(ValueError):
    pass


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def read_table(data: bytes, file_name: str) -> tuple[list[str], list[list[object]]]:
    """The headings and the rows of the first sheet (Excel) or the file (CSV)."""
    name = file_name.lower()
    if name.endswith(".xlsx"):
        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        except Exception:  # openpyxl raises many kinds of errors for a damaged file
            raise ImportFileError("That Excel file could not be read. Save it again as .xlsx or as CSV.")
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    elif name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    else:
        raise ImportFileError("Upload an Excel file (.xlsx) or a CSV file.")

    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        raise ImportFileError("The file is empty.")
    headers = [str(c).strip() if c is not None else "" for c in rows[0]]
    body = rows[1:]
    if not body:
        raise ImportFileError("The file has headings but no patients.")
    if len(body) > MAX_ROWS:
        raise ImportFileError(f"The file has {len(body):,} rows. Import up to {MAX_ROWS:,} at a time.")
    width = len(headers)
    return headers, [(r + [None] * width)[:width] for r in body]


def suggest_mapping(headers: list[str]) -> dict[str, int | None]:
    """Which column each Denti field probably comes from, by its heading."""
    plain = [_plain(h) for h in headers]
    mapping: dict[str, int | None] = {}
    used: set[int] = set()
    for key, _, names in FIELDS:
        found = next((i for i, h in enumerate(plain) if h in names and i not in used), None)
        mapping[key] = found
        if found is not None:
            used.add(found)
    return mapping


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def clean_phone(value: object) -> str | None:
    """Digits only; +91, 91 and a leading 0 dropped from Indian numbers. None if it cannot be a phone."""
    text = _text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if 7 <= len(digits) <= 13 else None


_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y")


def clean_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def clean_gender(value: object) -> str | None:
    text = (_text(value) or "").lower()
    if text in ("m", "male", "man", "boy"):
        return "male"
    if text in ("f", "female", "woman", "girl"):
        return "female"
    if text in ("o", "other", "others"):
        return "other"
    return None


def clean_amount(value: object) -> int | None:
    """Rupees as whole paise: 1200, "1,200.50" or "₹ 1200". None if it is not an amount."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    text = _text(value)
    if text is None:
        return 0
    cleaned = re.sub(r"[₹,\s]|rs\.?|inr", "", text.lower())
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        return None


@dataclass
class RowCheck:
    row: int  # as numbered in the spreadsheet, headings being row 1
    name: str | None
    phone: str | None
    previous_id: str | None
    kind: str  # ok, duplicate or invalid
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)


def check_rows(
    rows: list[list[object]],
    mapping: dict[str, int | None],
    *,
    existing: dict[tuple[str, str], str],
    known_conditions: list[str],
    today: date,
) -> list[RowCheck]:
    """Checks every row. `existing` maps (phone, lowercased name) to the patient ID already in Denti."""
    by_lower = {c.lower(): c for c in known_conditions}
    seen: dict[tuple[str, str], int] = {}
    checks = []
    for index, raw in enumerate(rows):
        def get(key: str):
            column = mapping.get(key)
            return raw[column] if column is not None and column < len(raw) else None

        row_number = index + 2
        name = _text(get("name"))
        raw_phone = _text(get("phone"))
        phone = clean_phone(raw_phone)
        check = RowCheck(row=row_number, name=name, phone=phone or raw_phone, previous_id=_text(get("previous_id")), kind="ok")

        if not name:
            check.problems.append("Name is missing")
        if not raw_phone:
            check.problems.append("Phone is missing")
        elif phone is None:
            check.problems.append("Phone number looks wrong")

        dob = clean_date(get("dob"))
        if get("dob") not in (None, "") and dob is None:
            check.warnings.append("Date of birth not understood, left empty")
        if dob and dob > today:
            check.warnings.append("Date of birth is in the future, left empty")
            dob = None
        birth_year = None
        age_text = _text(get("age"))
        if dob is None and age_text:
            digits = re.match(r"\d{1,3}", age_text)
            if digits and 0 <= int(digits.group()) <= 120:
                birth_year = today.year - int(digits.group())
            else:
                check.warnings.append("Age not understood, left empty")

        balance = clean_amount(get("opening_balance"))
        if balance is None:
            check.problems.append("Balance owed is not an amount")

        conditions, unknown = [], []
        for part in re.split(r"[,;/]", _text(get("medical_conditions")) or ""):
            part = part.strip()
            if part:
                (conditions if part.lower() in by_lower else unknown).append(by_lower.get(part.lower(), part))
        history = _text(get("medical_history"))
        if unknown:
            # Conditions not in the clinic's list are kept, in the history, so nothing is lost.
            history = "; ".join(x for x in [history, f"Other conditions: {', '.join(unknown)}"] if x)

        gender_text = _text(get("gender"))
        gender = clean_gender(gender_text)
        if gender_text and gender is None:
            check.warnings.append("Gender not understood, left empty")

        check.values = {
            "name": name, "phone": phone, "previous_id": check.previous_id, "gender": gender, "dob": dob,
            "birth_year": birth_year, "email": _text(get("email")), "city": _text(get("city")), "area": _text(get("area")),
            "medical_conditions": sorted(set(conditions)), "medical_history": history, "opening_balance": balance or 0,
        }

        if check.problems:
            check.kind = "invalid"
        elif name and phone:
            key = (phone, name.lower())
            if key in seen:
                check.kind = "duplicate"
                check.problems.append(f"Same patient as row {seen[key]}")
            elif key in existing:
                check.kind = "duplicate"
                check.problems.append(f"Already in Denti as {existing[key]}")
            seen.setdefault(key, row_number)
        checks.append(check)
    return checks
