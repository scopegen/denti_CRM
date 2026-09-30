import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, field_validator, model_validator

# Request and response shapes of the API. Separate from the database models on purpose.

# A simple shape check only. Whether the address really belongs to the person is proven
# by the code emailed at every sign in.
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]


def _password_fits_bcrypt(value: str) -> str:
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password is too long.")
    return value


class ClinicPublicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    slug: str
    name: str


class ClinicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    plan: str
    timezone: str


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    role: str
    treats_patients: bool
    qualification: str | None
    specialty: str | None
    registration_no: str | None
    status: str
    signature_file_id: uuid.UUID | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def has_signature(self) -> bool:
        return self.signature_file_id is not None


class SignatureIn(BaseModel):
    """A signature as a data URL: a photo of it on paper, or one drawn on screen."""

    image_data: str = Field(min_length=30, max_length=7_200_000)


# Signing in

class LoginRequest(BaseModel):
    clinic: str = Field(min_length=1, max_length=40)
    email: Email
    password: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    access_token: str
    staff: StaffOut
    clinic: ClinicOut


class LoginResponse(BaseModel):
    status: Literal["code_sent", "signed_in"]
    challenge_id: uuid.UUID | None = None
    sent_to: str | None = None
    resend_after_seconds: int | None = None
    session: SessionOut | None = None


class VerifyRequest(BaseModel):
    clinic: str = Field(min_length=1, max_length=40)
    challenge_id: uuid.UUID
    code: str = Field(pattern=r"^\d{6}$")
    trust_device: bool = False


class ClinicRequest(BaseModel):
    """Refreshing and signing out name the clinic, because each clinic has its own sign in cookie."""

    clinic: str = Field(min_length=1, max_length=40)


class ResendRequest(BaseModel):
    clinic: str = Field(min_length=1, max_length=40)
    challenge_id: uuid.UUID


class MeOut(BaseModel):
    staff: StaffOut
    clinic: ClinicOut


# Passwords

class PasswordResetRequest(BaseModel):
    clinic: str = Field(min_length=1, max_length=40)
    email: Email


class PasswordResetStarted(BaseModel):
    """The same answer whether or not the email has a login, so nobody can find out which do."""

    challenge_id: uuid.UUID
    resend_after_seconds: int


class PasswordResetConfirm(BaseModel):
    clinic: str = Field(min_length=1, max_length=40)
    challenge_id: uuid.UUID
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8, max_length=72)

    _check_password = field_validator("new_password")(_password_fits_bcrypt)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=72)

    _check_password = field_validator("new_password")(_password_fits_bcrypt)


# Staff

class StaffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: Email
    role: Literal["admin", "doctor"]
    password: str = Field(min_length=8, max_length=72)
    phone: str | None = Field(default=None, max_length=20)
    qualification: str | None = Field(default=None, max_length=120)
    specialty: str | None = Field(default=None, max_length=120)
    registration_no: str | None = Field(default=None, max_length=60)

    _check_password = field_validator("password")(_password_fits_bcrypt)


# Patients

Gender = Literal["male", "female", "other"]


class PatientIn(BaseModel):
    """What the registration and edit forms send. Either a full date of birth or a birth
    year is required (an age typed on the form is sent as a birth year)."""

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=20, pattern=r"^\+?[0-9 ]{7,20}$")
    email: Email | None = None
    gender: Gender | None = None
    dob: date | None = None
    birth_year: int | None = Field(default=None, ge=1900)
    city: str | None = Field(default=None, max_length=80)
    area: str | None = Field(default=None, max_length=80)
    height_cm: Decimal | None = Field(default=None, gt=0, lt=300)
    weight_kg: Decimal | None = Field(default=None, gt=0, lt=500)
    medical_conditions: list[str] = []
    medical_history: str | None = Field(default=None, max_length=4000)
    whatsapp_consent: bool = False

    @field_validator("email", "city", "area", "medical_history", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def _trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_dates(self) -> "PatientIn":
        today = date.today()
        if self.dob is None and self.birth_year is None:
            raise ValueError("Enter a date of birth, an age or a birth year.")
        if self.dob is not None and self.dob > today:
            raise ValueError("Date of birth cannot be in the future.")
        if self.birth_year is not None and self.birth_year > today.year:
            raise ValueError("Birth year cannot be in the future.")
        return self


class PatientCreate(PatientIn):
    # Family members often share one phone number, so staff may confirm and register anyway.
    allow_duplicate_phone: bool = False


class PatientOut(BaseModel):
    id: uuid.UUID
    patient_number: int
    code: str
    name: str
    phone: str
    email: str | None
    gender: str | None
    dob: date | None
    birth_year: int | None
    city: str | None
    area: str | None
    height_cm: float | None
    weight_kg: float | None
    medical_conditions: list[str]
    medical_history: str | None
    whatsapp_consent: bool
    registered_at: datetime
    updated_at: datetime | None


class PatientSummary(BaseModel):
    id: uuid.UUID
    patient_number: int
    code: str
    name: str
    phone: str
    gender: str | None
    dob: date | None
    birth_year: int | None
    medical_conditions: list[str]
    registered_at: datetime


class PatientPage(BaseModel):
    items: list[PatientSummary]
    total: int
    page: int
    page_size: int


class PatientStats(BaseModel):
    total: int
    new_this_month: int
    # Every patient ever registered counts towards the plan's limit.
    used: int
    limit: int | None


# Today page, arrivals and follow ups

class PersonBrief(BaseModel):
    id: uuid.UUID
    name: str


class PatientBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    phone: str
    medical_conditions: list[str]


class ArrivalCreate(BaseModel):
    patient_id: uuid.UUID
    preferred_doctor_id: uuid.UUID | None = None


class ArrivalOut(BaseModel):
    id: uuid.UUID
    patient: PatientBrief
    arrived_at: datetime
    preferred_doctor: PersonBrief | None
    seen_at: datetime | None


class FollowUpCreate(BaseModel):
    kind: Literal["call", "visit"]
    due_on: date
    due_time: time | None = None
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value.strip() if isinstance(value, str) else value


class FollowUpOut(BaseModel):
    id: uuid.UUID
    kind: str
    due_on: date
    due_time: time | None
    reason: str | None
    status: str
    source: str
    created_at: datetime
    completed_at: datetime | None
    overdue: bool


class FollowUpItem(FollowUpOut):
    patient: PatientBrief
    arrived_today: bool = False


class TodayOut(BaseModel):
    date: date
    expected: list[FollowUpItem]
    calls: list[FollowUpItem]
    arrived: list[ArrivalOut]


# Consultations and prescriptions

def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class RxItem(BaseModel):
    """One medicine line: the medicine is required, the rest is optional."""

    medicine: str = Field(min_length=1, max_length=120)
    dose: str | None = Field(default=None, max_length=60)
    frequency: str | None = Field(default=None, max_length=30)
    duration: str | None = Field(default=None, max_length=40)
    instructions: str | None = Field(default=None, max_length=120)

    @field_validator("medicine", "dose", "frequency", "duration", "instructions", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class PrescriptionIn(BaseModel):
    diagnosis: str | None = Field(default=None, max_length=1000)
    advice: str | None = Field(default=None, max_length=2000)
    items: list[RxItem] = Field(default=[], max_length=30)

    @field_validator("diagnosis", "advice", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)

    def is_empty(self) -> bool:
        return not self.items and not self.diagnosis and not self.advice


class RecommendationIn(BaseModel):
    service_id: uuid.UUID
    teeth: list[int] = []

    @field_validator("teeth")
    @classmethod
    def _valid_teeth(cls, value: list[int]) -> list[int]:
        from app.teeth import check_teeth

        return check_teeth(value)


class FindingIn(BaseModel):
    """A problem on a tooth, from the clinic's tooth_problem list, or 'missing'."""

    tooth: int
    problem: str = Field(min_length=1, max_length=120)


class FindingBrief(BaseModel):
    id: uuid.UUID
    tooth: int
    problem: str
    # 'treated', 'cleared', or null while it is still a problem.
    resolution: str | None


class ConsultationIn(BaseModel):
    doctor_id: uuid.UUID
    # Defaults to today in the clinic's time zone. An earlier date is allowed, for entering
    # history by hand; a future date is not.
    consult_date: date | None = None
    chief_complaint: str = Field(min_length=1, max_length=4000)
    oral_examination: str = Field(min_length=1, max_length=4000)
    xray_taken: bool = False
    recommendations: list[RecommendationIn] = Field(default=[], max_length=30)
    recommendation_note: str | None = Field(default=None, max_length=2000)
    prescription: PrescriptionIn | None = None
    # Problems marked on the tooth chart during this consultation (Standard and Pro). On an
    # edit this is the full list: ones left out are cleared.
    findings: list[FindingIn] = Field(default=[], max_length=64)

    @field_validator("chief_complaint", "oral_examination", "recommendation_note", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class RecommendationOut(BaseModel):
    service_id: uuid.UUID
    service_name: str
    teeth: list[int]


class PrescriptionOut(BaseModel):
    id: uuid.UUID
    prescribing_doctor: PersonBrief
    version: int
    diagnosis: str | None
    advice: str | None
    items: list[RxItem]
    edited_at: datetime
    # Whether the person asking may edit it: only the prescribing doctor can.
    can_edit: bool


class PrescriptionVersionOut(BaseModel):
    version: int
    diagnosis: str | None
    advice: str | None
    items: list[RxItem]
    edited_by: PersonBrief
    edited_at: datetime


class ConsultationOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor: PersonBrief
    consult_date: date
    chief_complaint: str
    oral_examination: str
    xray_taken: bool
    recommendations: list[RecommendationOut]
    recommendation_note: str | None
    prescription: PrescriptionOut | None
    findings: list[FindingBrief]
    created_at: datetime
    updated_at: datetime | None


# Services

class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    kind: Literal["in_house", "lab"] = "in_house"
    listed_price_paise: int = Field(ge=0, le=10_000_000_000)
    recall_after_months: int | None = Field(default=None, ge=1, le=60)
    # An extraction: finishing it marks the tooth as missing on the tooth chart.
    removes_tooth: bool = False
    active: bool = True

    @field_validator("name", "category", mode="before")
    @classmethod
    def _trim(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    category: str | None
    kind: str
    # Null for doctors, who never see prices.
    listed_price_paise: int | None
    recall_after_months: int | None
    removes_tooth: bool
    active: bool


class StaffUpdate(BaseModel):
    """Owner and receptionist edit a person's details. Email and role changes are not
    offered; a new person gets a new login."""

    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    qualification: str | None = Field(default=None, max_length=120)
    specialty: str | None = Field(default=None, max_length=120)
    registration_no: str | None = Field(default=None, max_length=60)

    @field_validator("name", "phone", "qualification", "specialty", "registration_no", mode="before")
    @classmethod
    def _trim(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


# Audit log

class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    patient_id: uuid.UUID | None
    changes: dict | None
    ip: str | None
    device: str | None
    occurred_at: datetime


class AuditPatient(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class AuditEntryView(BaseModel):
    """One line of the Activity log, with names instead of ids."""

    id: int
    action: str
    actor_type: str
    actor: PersonBrief | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    patient: AuditPatient | None
    changes: dict | None
    ip: str | None
    device: str | None
    occurred_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEntryView]
    # Pass as before_id to get the next, older page; null when there is no more.
    next_before_id: int | None
    people: list[PersonBrief]


# Plan and usage

class Meter(BaseModel):
    used: int
    limit: int | None  # None: unlimited on this plan


class PlanUsage(BaseModel):
    patients: Meter
    receptionists: Meter
    doctors: Meter
    storage_bytes: Meter


class BillingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    billing_cycle: Literal["starter", "monthly", "yearly"]
    status: str
    current_period_start: datetime
    current_period_end: datetime
    storage_addon_packs: int


class PlanOption(BaseModel):
    key: str
    name: str
    receptionists: int | None
    doctors: int | None
    patients: int | None
    storage_gb: int
    starter_pack_paise: int
    monthly_paise: int
    yearly_paise: int


class PlanFeature(BaseModel):
    label: str
    plans: list[str]


class PlanChangeOut(BaseModel):
    id: uuid.UUID
    kind: Literal["upgrade", "downgrade"]
    from_plan: str
    from_plan_name: str
    to_plan: str
    to_plan_name: str
    status: Literal["pending", "applied", "cancelled"]
    effective_at: datetime
    amount_paise: int
    gst_paise: int
    total_paise: int
    payment_method: str | None
    requested_by: PersonBrief
    requested_at: datetime


class PlanOut(BaseModel):
    plan: str
    plan_name: str
    billing: BillingOut | None
    usage: PlanUsage
    plans: list[PlanOption]
    features: list[PlanFeature]
    starter_pack_months: int
    storage_pack_gb: int
    storage_pack_monthly_paise: int
    gst_percent: int
    # A move to another plan waiting for its date (moving down) or its payment.
    pending_change: PlanChangeOut | None
    # Only the owner changes the plan.
    can_change: bool


class SeatChoice(BaseModel):
    """Moving down to fewer logins of one kind: who is active now, and who stays if the
    owner does not choose (the longest serving)."""

    role: Literal["admin", "doctor"]
    limit: int
    active: list[PersonBrief]
    suggested_keep: list[uuid.UUID]


class PlanChangePreview(BaseModel):
    to_plan: str
    to_plan_name: str
    kind: Literal["upgrade", "downgrade"]
    # Moving up: now. Moving down: when the paid period ends.
    effective_at: datetime
    amount_paise: int
    gst_paise: int
    total_paise: int
    # What the clinic pays from its next renewal on the new plan.
    next_price_paise: int
    next_billing_cycle: Literal["monthly", "yearly"]
    gained: list[str]
    lost: list[str]
    seats: list[SeatChoice]
    # Moving down past a limit: nothing is removed, but new patients or uploads stop.
    patients: Meter | None
    storage_bytes: Meter | None
    # "test" means no money is taken (online payment is not connected yet).
    payment_method: str


class PlanChangeIn(BaseModel):
    to_plan: Literal["basic", "standard", "pro"]
    keep_staff_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


# X-rays, photos and documents on a patient's record

PatientFileKind = Literal["xray", "photo", "document"]


class UploadRequest(BaseModel):
    """Step one of an upload: what is about to be sent."""

    content_type: str = Field(max_length=100)
    size_bytes: int = Field(gt=0)


class UploadTicket(BaseModel):
    """Where to send the bytes (step two), and the token that confirms them (step three)."""

    upload_token: str
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


class PatientFileDetails(BaseModel):
    kind: PatientFileKind
    title: str | None = Field(default=None, max_length=200)
    teeth: list[int] = Field(default=[], max_length=32)

    @field_validator("title", mode="before")
    @classmethod
    def _trim(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("teeth")
    @classmethod
    def _valid_teeth(cls, value: list[int]) -> list[int]:
        from app.teeth import check_teeth

        return check_teeth(value)


class PatientFileCreate(PatientFileDetails):
    upload_token: str


class PatientFileOut(BaseModel):
    id: uuid.UUID
    kind: PatientFileKind
    title: str | None
    teeth: list[int]
    content_type: str
    size_bytes: int
    width: int | None
    height: int | None
    uploaded_by: PersonBrief
    uploaded_at: datetime
    # In the bin: when it was moved there, and when it is removed for good.
    deleted_at: datetime | None
    purge_after: datetime | None
    # Signed links that work in an <img> or a new tab without the sign in, for a short time.
    view_url: str
    download_url: str
    thumbnail_url: str | None


# Treatments and visits

AdjustmentType = Literal["percent", "amount"]


def _checked_teeth(value: list[int]) -> list[int]:
    from app.teeth import check_teeth

    return check_teeth(value)


class TreatmentCreate(BaseModel):
    """Any of the clinic's services, recommended in a consultation or not."""

    service_id: uuid.UUID
    doctor_id: uuid.UUID
    teeth: list[int] = Field(default=[], max_length=32)
    # Set when started from a consultation's recommendation.
    consultation_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    # Start now (the charge goes on the bill) or only plan it.
    start: bool = False
    started_on: date | None = None
    # Owner and receptionist only; everyone else gets the service's listed price.
    price_paise: int | None = Field(default=None, ge=0, le=10_000_000_000)

    @field_validator("notes", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)

    @field_validator("teeth")
    @classmethod
    def _valid_teeth(cls, value: list[int]) -> list[int]:
        return _checked_teeth(value)


class TreatmentUpdate(BaseModel):
    """Clinical details anyone may change. The service can change only while planned."""

    service_id: uuid.UUID
    teeth: list[int] = Field(default=[], max_length=32)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)

    @field_validator("teeth")
    @classmethod
    def _valid_teeth(cls, value: list[int]) -> list[int]:
        return _checked_teeth(value)


class TreatmentPriceIn(BaseModel):
    """Owner and receptionist only. Percentages are in basis points: 1000 is 10%."""

    price_paise: int = Field(ge=0, le=10_000_000_000)
    adjustment_type: AdjustmentType | None = None
    adjustment_value: int = Field(default=0, ge=0, le=10_000_000_000)
    discount_type: AdjustmentType | None = None
    discount_value: int = Field(default=0, ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def _consistent(self):
        if self.adjustment_type is None:
            self.adjustment_value = 0
        if self.discount_type is None:
            self.discount_value = 0
        if self.discount_type == "percent" and self.discount_value > 10000:
            raise ValueError("A discount cannot be more than 100%.")
        return self


class TreatmentDay(BaseModel):
    """Starting or finishing: the day it happened, today unless entered later."""

    on: date | None = None


class TreatmentCancel(BaseModel):
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class HandoffIn(BaseModel):
    to_doctor_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class NextVisitIn(BaseModel):
    due_on: date
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class VisitIn(BaseModel):
    """One sitting. Saving it can also finish the treatment and book the next sitting."""

    visit_date: date | None = None
    doctor_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=4000)
    prescription: PrescriptionIn | None = None
    finish_treatment: bool = False
    next_visit: NextVisitIn | None = None

    @field_validator("notes", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class VisitUpdate(BaseModel):
    visit_date: date
    doctor_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=4000)
    prescription: PrescriptionIn | None = None

    @field_validator("notes", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class VisitOut(BaseModel):
    id: uuid.UUID
    visit_date: date
    doctor: PersonBrief
    notes: str | None
    prescription: PrescriptionOut | None
    created_at: datetime
    updated_at: datetime | None


class HandoffOut(BaseModel):
    from_doctor: PersonBrief
    to_doctor: PersonBrief
    changed_by: PersonBrief
    changed_at: datetime
    reason: str | None


class TreatmentMoney(BaseModel):
    """Only for the owner and receptionist. Doctors get null instead."""

    price_paise: int
    adjustment_type: str | None
    adjustment_value: int
    discount_type: str | None
    discount_value: int
    final_price_paise: int
    # What is on the bill for it now: nothing while planned.
    billed_paise: int


class ServiceBrief(BaseModel):
    id: uuid.UUID
    name: str


class TreatmentOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    service: ServiceBrief
    teeth: list[int]
    doctor: PersonBrief
    status: Literal["planned", "ongoing", "finished", "cancelled"]
    consultation_id: uuid.UUID | None
    notes: str | None
    started_on: date | None
    finished_on: date | None
    cancelled_on: date | None
    cancel_reason: str | None
    created_at: datetime
    visits: list[VisitOut]
    handoffs: list[HandoffOut]
    money: TreatmentMoney | None


class TreatmentSuggestion(BaseModel):
    """A consultation's recommendation that has not become a treatment yet."""

    consultation_id: uuid.UUID
    consult_date: date
    doctor: PersonBrief
    service: ServiceBrief
    teeth: list[int]


# The tooth chart (Standard and Pro)

class FindingCreate(FindingIn):
    consultation_id: uuid.UUID | None = None


class FindingOut(BaseModel):
    id: uuid.UUID
    tooth: int
    problem: str
    consultation_id: uuid.UUID | None
    noted_by: PersonBrief
    noted_at: datetime
    # 'treated' by a finished treatment, or 'cleared' by hand; null while it is a problem.
    resolution: Literal["treated", "cleared"] | None
    resolved_at: datetime | None
    resolved_by: PersonBrief | None
    resolved_by_treatment_id: uuid.UUID | None


class ChartTreatment(BaseModel):
    id: uuid.UUID
    service: ServiceBrief
    status: Literal["planned", "ongoing", "finished", "cancelled"]
    teeth: list[int]
    doctor: PersonBrief
    started_on: date | None
    finished_on: date | None
    created_at: datetime


class MouthCheckOut(BaseModel):
    checked_on: date
    checked_by: PersonBrief


class ToothChartOut(BaseModel):
    numbering: Literal["fdi", "universal"]
    # Only teeth with something recorded; the rest are healthy. Keys are FDI tooth numbers.
    statuses: dict[int, Literal["planned", "problem", "missing", "done"]]
    findings: list[FindingOut]
    treatments: list[ChartTreatment]
    last_mouth_check: MouthCheckOut | None
    # The clinic's tooth_problem list, for the problem buttons.
    problems: list[str]


# The bill (owner and receptionist only)

MAX_AMOUNT_PAISE = 10_000_000_000


class LedgerOut(BaseModel):
    id: uuid.UUID
    kind: Literal[
        "consultation_fee", "treatment_charge", "price_change", "opening_balance", "payment", "payment_reversal", "refund"
    ]
    amount_paise: int
    effective_at: datetime
    # What the line is, in words: "Consultation fee", "Root canal treatment, tooth 36".
    description: str
    payment_mode: str | None
    note: str | None
    consultation_id: uuid.UUID | None
    treatment_id: uuid.UUID | None
    reverses_entry_id: uuid.UUID | None
    # For a payment: whether it has been reversed since.
    reversed: bool
    created_by: PersonBrief
    created_at: datetime
    # The balance after this line, with the lines in date order.
    balance_paise: int


class BillOut(BaseModel):
    # Positive: the patient owes this. Negative: the clinic holds this much in credit.
    outstanding_paise: int
    charged_paise: int
    paid_paise: int
    entries: list[LedgerOut]
    has_opening_balance: bool
    payment_modes: list[str]


class PaymentIn(BaseModel):
    amount_paise: int = Field(gt=0, le=MAX_AMOUNT_PAISE)
    payment_mode: str = Field(min_length=1, max_length=40)
    # Today unless entered later; never in the future.
    paid_on: date | None = None
    note: str | None = Field(default=None, max_length=300)

    @field_validator("note", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class RefundIn(BaseModel):
    amount_paise: int = Field(gt=0, le=MAX_AMOUNT_PAISE)
    payment_mode: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("note", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class ReversalIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class OpeningBalanceIn(BaseModel):
    """What the patient owed (positive) or had in credit (negative) in the clinic's old system."""

    amount_paise: int = Field(ge=-MAX_AMOUNT_PAISE, le=MAX_AMOUNT_PAISE)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("amount_paise")
    @classmethod
    def _not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Enter the amount owed, or the credit as a negative amount.")
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)


class DueRow(BaseModel):
    patient: PatientBrief
    outstanding_paise: int
    last_payment_at: datetime | None
    last_charge_at: datetime | None


class DuesOut(BaseModel):
    total_paise: int
    count: int
    items: list[DueRow]
    page: int
    page_size: int


# Invoices (owner and receptionist only)

class InvoiceableItem(BaseModel):
    """Something on the bill that is not on an invoice yet."""

    kind: Literal["consultation", "treatment"]
    id: uuid.UUID
    description: str
    teeth: list[int]
    on: date
    amount_paise: int


class InvoiceCreate(BaseModel):
    consultation_ids: list[uuid.UUID] = Field(default=[], max_length=100)
    treatment_ids: list[uuid.UUID] = Field(default=[], max_length=100)
    issued_on: date | None = None


class InvoiceLineOut(BaseModel):
    description: str
    teeth: list[int]
    amount_paise: int


class InvoiceOut(BaseModel):
    id: uuid.UUID
    # SMILE/26-27/0001
    number_text: str
    financial_year: str
    issued_on: date
    issued_by: PersonBrief
    total_paise: int
    status: Literal["issued", "cancelled"]
    cancelled_at: datetime | None
    cancel_reason: str | None
    lines: list[InvoiceLineOut]


class InvoicesOut(BaseModel):
    invoices: list[InvoiceOut]
    invoiceable: list[InvoiceableItem]


class InvoiceCancel(BaseModel):
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, value):
        return value.strip() if isinstance(value, str) else value


# Treatment estimates (quotes). The owner and receptionist make them; doctors see only the
# status and what is in them, never amounts.

class EstimateItemIn(BaseModel):
    service_id: uuid.UUID
    # Defaults to the service's name.
    description: str | None = Field(default=None, max_length=200)
    teeth: list[int] = Field(default=[], max_length=32)
    sittings: int | None = Field(default=None, ge=1, le=50)
    unit_price_paise: int = Field(ge=0, le=MAX_AMOUNT_PAISE)
    quantity: int = Field(default=1, ge=1, le=50)

    @field_validator("description", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)

    @field_validator("teeth")
    @classmethod
    def _valid_teeth(cls, value: list[int]) -> list[int]:
        return _checked_teeth(value)


class EstimateOptionIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    items: list[EstimateItemIn] = Field(min_length=1, max_length=40)

    @field_validator("label", mode="before")
    @classmethod
    def _trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class EstimateCreate(BaseModel):
    doctor_id: uuid.UUID
    # Defaults to the clinic's estimate validity (30 days) from today.
    valid_until: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    discount_type: AdjustmentType | None = None
    discount_value: int = Field(default=0, ge=0, le=MAX_AMOUNT_PAISE)
    options: list[EstimateOptionIn] = Field(min_length=1, max_length=4)

    @field_validator("notes", mode="before")
    @classmethod
    def _trim(cls, value):
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _consistent(self):
        if self.discount_type is None:
            self.discount_value = 0
        if self.discount_type == "percent" and self.discount_value > 10000:
            raise ValueError("A discount cannot be more than 100%.")
        return self


class EstimateItemOut(BaseModel):
    id: uuid.UUID
    service_id: uuid.UUID
    description: str
    teeth: list[int]
    sittings: int | None
    quantity: int
    # Null for doctors.
    unit_price_paise: int | None
    amount_paise: int | None


class EstimateOptionOut(BaseModel):
    id: uuid.UUID
    label: str
    items: list[EstimateItemOut]
    # Null for doctors.
    subtotal_paise: int | None
    discount_paise: int | None
    total_paise: int | None


class EstimateOut(BaseModel):
    id: uuid.UUID
    # EST/2026-27/0001
    number_text: str
    given_on: date
    valid_until: date
    status: Literal["given", "accepted", "declined", "expired"]
    doctor: PersonBrief
    # Null for doctors.
    notes: str | None
    discount_type: str | None
    discount_value: int | None
    options: list[EstimateOptionOut]
    accepted_option_id: uuid.UUID | None
    decided_at: datetime | None


class EstimateAccept(BaseModel):
    option_id: uuid.UUID


# Patient import (owner and receptionist only)

class ImportUpload(BaseModel):
    file_name: str = Field(min_length=1, max_length=200)
    # The file itself, base64 encoded. Up to 5 MB.
    data_base64: str = Field(min_length=4, max_length=7_000_000)


class ImportField(BaseModel):
    key: str
    label: str


class ImportPreview(BaseModel):
    file_id: uuid.UUID
    file_name: str
    columns: list[str]
    fields: list[ImportField]
    # Denti field to column number (0 = the first column), or null when no column matches.
    mapping: dict[str, int | None]
    sample: list[list[str]]
    row_count: int


class ImportMappingIn(BaseModel):
    file_id: uuid.UUID
    mapping: dict[str, int | None]
    keep_numbers: bool = False


class ImportRun(ImportMappingIn):
    # Rows flagged as duplicates that should be imported anyway (as numbered in the file).
    include_rows: list[int] = Field(default=[], max_length=5000)


class ImportRowOut(BaseModel):
    row: int
    name: str | None
    phone: str | None
    previous_id: str | None
    kind: Literal["ok", "duplicate", "invalid"]
    problems: list[str]
    warnings: list[str]


class PlanFit(BaseModel):
    used: int
    adding: int
    limit: int | None
    fits: bool
    # The smallest plan the clinic would fit on, when it does not fit now.
    suggested_plan: str | None
    suggested_plan_name: str | None


class ImportCheckOut(BaseModel):
    ok: int
    duplicates: int
    invalid: int
    # Only rows that need a look: duplicates, rows that cannot be imported, and warnings.
    flagged: list[ImportRowOut]
    plan: PlanFit
    keep_numbers_possible: bool
    keep_numbers_reason: str | None


class ImportBatchOut(BaseModel):
    id: uuid.UUID
    file_name: str
    rows_total: int
    rows_imported: int
    rows_skipped: int
    kept_numbers: bool
    uploaded_by: PersonBrief
    created_at: datetime
    undo_deadline: datetime
    undone_at: datetime | None
    can_undo: bool


# WhatsApp

MessageKind = Literal[
    "visit_reminder", "call_reminder", "general", "prescription", "invoice", "estimate", "review_request", "lab_ready"
]


class MessageTemplateOut(BaseModel):
    kind: MessageKind
    language: Literal["en", "hi"]
    body: str
    default_body: str
    is_default: bool
    placeholders: list[str]


class WhatsAppSettingsOut(BaseModel):
    language: Literal["en", "hi"]
    google_review_url: str | None
    templates: list[MessageTemplateOut]


class WhatsAppSettingsIn(BaseModel):
    language: Literal["en", "hi"]
    google_review_url: str | None = Field(default=None, max_length=500)

    @field_validator("google_review_url")
    @classmethod
    def _review_link(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if not value.startswith("https://"):
            raise ValueError("The review link should start with https://")
        return value


class MessageTemplateIn(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


class MessageIn(BaseModel):
    """Which message to open for which patient, and what it is about."""

    patient_id: uuid.UUID
    kind: MessageKind
    # The clinic's usual language when left out.
    language: Literal["en", "hi"] | None = None
    follow_up_id: uuid.UUID | None = None
    treatment_id: uuid.UUID | None = None
    prescription_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    estimate_id: uuid.UUID | None = None


class MessageOpened(BaseModel):
    url: str
    phone: str
    body: str
    link_expires_at: datetime | None


class ShareLinkOut(BaseModel):
    id: uuid.UUID
    expires_at: datetime
    revoked_at: datetime | None
    open_count: int
    last_opened_at: datetime | None
    active: bool


class MessageOut(BaseModel):
    id: uuid.UUID
    kind: MessageKind
    language: Literal["en", "hi"]
    body: str
    to_phone: str
    sent_by: PersonBrief | None
    created_at: datetime
    link: ShareLinkOut | None


class SharedDocumentOut(BaseModel):
    """What a patient sees before opening a shared document. Nothing beyond what the
    document itself shows."""

    clinic_name: str
    kind: Literal["prescription", "invoice", "estimate"]
    title: str
    date_text: str
    expires_at: datetime
