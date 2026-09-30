from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.models import Clinic


def clinic_now(clinic: Clinic) -> datetime:
    return datetime.now(ZoneInfo(clinic.timezone))


def clinic_today(clinic: Clinic) -> date:
    """Today in the clinic's own time zone, so the day never changes at midnight UTC."""
    return clinic_now(clinic).date()


def add_months(moment: datetime, months: int) -> datetime:
    """The same day and time some months later; 31 Jan plus one month is 28 or 29 Feb."""
    month_index = moment.month - 1 + months
    year, month = moment.year + month_index // 12, month_index % 12 + 1
    days_in_month = (date(year + month // 12, month % 12 + 1, 1) - date(year, month, 1)).days
    return moment.replace(year=year, month=month, day=min(moment.day, days_in_month))
