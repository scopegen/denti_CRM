from dataclasses import dataclass

# Denti's plans: what each includes and what it costs. The server enforces the limits and
# the Plan page shows them from here, so there is one source for both.


@dataclass(frozen=True)
class PlanLimits:
    """None means unlimited. The owner login is free on every plan and never counted."""

    receptionists: int | None
    doctors: int | None
    patients: int | None
    storage_gb: int


PLANS: dict[str, PlanLimits] = {
    "basic": PlanLimits(receptionists=1, doctors=1, patients=500, storage_gb=3),
    "standard": PlanLimits(receptionists=1, doctors=5, patients=1500, storage_gb=15),
    "pro": PlanLimits(receptionists=None, doctors=None, patients=None, storage_gb=40),
}

PLAN_NAMES = {"basic": "Basic", "standard": "Standard", "pro": "Pro"}


@dataclass(frozen=True)
class PlanPrices:
    """Whole paise, before GST. The Starter pack is one payment for the first 3 months."""

    starter_pack: int
    monthly: int
    yearly: int


PRICES: dict[str, PlanPrices] = {
    "basic": PlanPrices(starter_pack=60000, monthly=99900, yearly=999000),
    "standard": PlanPrices(starter_pack=80000, monthly=149900, yearly=1499000),
    "pro": PlanPrices(starter_pack=110000, monthly=249900, yearly=2499000),
}

STARTER_PACK_MONTHS = 3
GST_PERCENT = 18

# Extra storage on any plan, bought in packs.
STORAGE_PACK_GB = 50
STORAGE_PACK_MONTHLY = 49900

GB = 1024**3

_ALL = ("basic", "standard", "pro")
_STANDARD_UP = ("standard", "pro")

# What each plan includes, in the order the Plan page lists it.
FEATURES: list[tuple[str, tuple[str, ...]]] = [
    ("Patients, consultations, prescriptions and bills", _ALL),
    ("X-rays and photos", _ALL),
    ("Activity log for the owner", _ALL),
    ("Daily closing, dues list and full export", _ALL),
    ("Tooth chart", _STANDARD_UP),
    ("WhatsApp reminders", _STANDARD_UP),
    ("Lab work tracking", _STANDARD_UP),
    ("Monthly summary, reports by doctor and treatment, Excel", _STANDARD_UP),
    ("Letterhead designs and brand colour", _STANDARD_UP),
    ("Doctor payouts, treatment acceptance, monthly report by email", ("pro",)),
    ('Remove "Powered by Denti" from documents', ("pro",)),
]


def storage_limit_bytes(plan: str, storage_packs: int = 0) -> int:
    return (PLANS[plan].storage_gb + storage_packs * STORAGE_PACK_GB) * GB
