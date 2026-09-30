import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.ids import uuid7

# Tables and rules are described in docs/database-design.md. Every table except clinics
# has row level security: a query only sees rows of the clinic set for the transaction.


# Each clinic lives at denti.in/<slug>/..., so a slug may not be a word Denti uses for its own pages.
RESERVED_CLINIC_SLUGS = (
    "api", "app", "admin", "assets", "fonts", "images", "login", "signup", "style-guide",
    "www", "help", "support", "pricing", "about", "contact", "terms", "privacy",
)
RESERVED_SLUGS_SQL = ", ".join(f"'{slug}'" for slug in RESERVED_CLINIC_SLUGS)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Clinic(Base):
    """One row per customer clinic. Shared by all of Denti, so no row level security;
    the API role can only read it."""

    __tablename__ = "clinics"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'", name="clinics_slug_format"),
        CheckConstraint(f"slug NOT IN ({RESERVED_SLUGS_SQL})", name="clinics_slug_not_reserved"),
        CheckConstraint("status IN ('active', 'past_due', 'frozen', 'closed')", name="clinics_status"),
        CheckConstraint("plan IN ('basic', 'standard', 'pro')", name="clinics_plan"),
        CheckConstraint("tooth_numbering IN ('fdi', 'universal')", name="clinics_tooth_numbering"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="basic")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    patient_id_prefix: Mapped[str | None] = mapped_column(String(12))
    tooth_numbering: Mapped[str] = mapped_column(String(10), nullable=False, default="fdi")
    created_at: Mapped[datetime] = created_at()


class Staff(Base):
    __tablename__ = "staff"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="staff_clinic_id_id_key"),
        CheckConstraint("role IN ('owner', 'admin', 'doctor')", name="staff_role"),
        CheckConstraint("status IN ('active', 'paused', 'deactivated')", name="staff_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    treats_patients: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    qualification: Mapped[str | None] = mapped_column(String(120))
    specialty: Mapped[str | None] = mapped_column(String(120))
    registration_no: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Bumped to sign this person out everywhere at once.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A transparent PNG in file storage, printed on prescriptions.
    signature_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at()


def staff_fk() -> ForeignKeyConstraint:
    # Composite key: the staff member must belong to the same clinic as the row.
    return ForeignKeyConstraint(["clinic_id", "staff_id"], ["staff.clinic_id", "staff.id"])


class AuthSession(Base):
    """A signed in browser. The refresh token lives in an HttpOnly cookie; only its hash
    is stored here, so sessions can be revoked."""

    __tablename__ = "auth_sessions"
    __table_args__ = (staff_fk(),)

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device: Mapped[str | None] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginCode(Base):
    """A 6 digit code sent by email, for signing in or resetting a password. Stored as a
    keyed hash, never in plain text."""

    __tablename__ = "login_codes"
    __table_args__ = (
        staff_fk(),
        CheckConstraint("purpose IN ('sign_in', 'password_reset')", name="login_codes_purpose"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_to: Mapped[str] = mapped_column(String(255), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(200))


class TrustedDevice(Base):
    """A browser where the person ticked "Trust this device for 30 days", so signing in
    there skips the emailed code."""

    __tablename__ = "trusted_devices"
    __table_args__ = (staff_fk(),)

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    device_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Counter(Base):
    """Per clinic running numbers: patient numbers, and invoice and estimate numbers per
    financial year. Taken with a row lock so two people never get the same number."""

    __tablename__ = "counters"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Picklist(Base):
    """Lists each clinic edits: medical conditions, tooth problems, payment modes and so on."""

    __tablename__ = "picklists"

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    list: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at()


class Patient(Base):
    """Never deleted. Two records of the same person are merged instead (merged_into_id)."""

    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="patients_clinic_id_id_key"),
        UniqueConstraint("clinic_id", "patient_number", name="patients_clinic_number_key"),
        ForeignKeyConstraint(["clinic_id", "registered_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_id: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    gender: Mapped[str | None] = mapped_column(String(10))
    dob: Mapped[date | None] = mapped_column(Date)
    birth_year: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(80))
    area: Mapped[str | None] = mapped_column(String(80))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    medical_conditions: Mapped[list[str]] = mapped_column(ARRAY(String(120)), nullable=False, default=list)
    medical_history: Mapped[str | None] = mapped_column(Text)
    whatsapp_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Service(Base):
    """The clinic's catalog. Prices are whole paise. Retired (active false), never deleted,
    because treatments point at them."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="services_clinic_id_id_key"),
        CheckConstraint("kind IN ('in_house', 'lab')", name="services_kind"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="in_house")
    listed_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recall_after_months: Mapped[int | None] = mapped_column(SmallInteger)
    # An extraction: finishing it marks the tooth as missing on the tooth chart.
    removes_tooth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Arrival(Base):
    """A walk in on the Today page. Waiting until a consultation or visit is saved for the
    patient that day, which sets seen_at. Cancelled, never deleted, if marked by mistake."""

    __tablename__ = "arrivals"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "preferred_doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "marked_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    clinic_date: Mapped[date] = mapped_column(Date, nullable=False)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    preferred_doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    marked_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_by_consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    seen_by_visit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class FollowUp(Base):
    """A call to make or a visit to expect, on a date. Kept as a history: done and cancelled
    follow ups stay on the patient's record."""

    __tablename__ = "follow_ups"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "completed_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        UniqueConstraint("clinic_id", "id", name="follow_ups_clinic_id_id_key"),
        CheckConstraint("kind IN ('call', 'visit')", name="follow_ups_kind"),
        CheckConstraint("status IN ('upcoming', 'done', 'cancelled')", name="follow_ups_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_time: Mapped[time | None] = mapped_column(Time)
    reason: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="upcoming")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # Set for a visit booked at the end of a sitting, or a recall after a finished treatment.
    treatment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AuditLog(Base):
    """Append only: the API role may insert and read, never update or delete."""

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("actor_type IN ('staff', 'platform_support', 'system', 'patient')", name="audit_log_actor_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    changes: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ClinicSettings(Base):
    """One row per clinic. Created with defaults the first time it is read."""

    __tablename__ = "clinic_settings"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), primary_key=True)
    default_consultation_fee_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=50000)
    estimate_valid_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # WhatsApp: the language messages open in, and where review requests send patients.
    whatsapp_language: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    google_review_url: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consultation(Base):
    """The exam. Never deleted; every edit keeps its before and after in the audit log."""

    __tablename__ = "consultations"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="consultations_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consult_date: Mapped[date] = mapped_column(Date, nullable=False)
    chief_complaint: Mapped[str] = mapped_column(Text, nullable=False)
    oral_examination: Mapped[str] = mapped_column(Text, nullable=False)
    xray_taken: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recommendation_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsultationRecommendation(Base):
    __tablename__ = "consultation_recommendations"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    consultation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    teeth: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)


class Prescription(Base):
    """One per consultation or per visit. Its words live in PrescriptionVersion rows; each
    real change adds a version, nothing is overwritten. Only the prescribing doctor edits it."""

    __tablename__ = "prescriptions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="prescriptions_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "visit_id"], ["visits.clinic_id", "visits.id"]),
        ForeignKeyConstraint(["clinic_id", "prescribing_doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    visit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prescribing_doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()


class PrescriptionVersion(Base):
    __tablename__ = "prescription_versions"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "prescription_id"], ["prescriptions.clinic_id", "prescriptions.id"]),
        ForeignKeyConstraint(["clinic_id", "edited_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    prescription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    advice: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    edited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Treatment(Base):
    """A treatment for a patient, from any of the clinic's services, recommended or not.
    Planned treatments are not billed; starting one adds its charge to the bill. Never
    deleted: a mistake is cancelled. The dates follow the status (CHECK constraints)."""

    __tablename__ = "treatments"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="treatments_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint("status IN ('planned', 'ongoing', 'finished', 'cancelled')", name="treatments_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    teeth: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    estimate_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="planned")
    price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adjustment_type: Mapped[str | None] = mapped_column(String(7))
    adjustment_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_type: Mapped[str | None] = mapped_column(String(7))
    discount_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    started_on: Mapped[date | None] = mapped_column(Date)
    finished_on: Mapped[date | None] = mapped_column(Date)
    cancelled_on: Mapped[date | None] = mapped_column(Date)
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Visit(Base):
    """One sitting of a treatment: when, which doctor, and what was done. It can carry its
    own prescription."""

    __tablename__ = "visits"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="visits_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        ForeignKeyConstraint(["clinic_id", "doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    treatment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TreatmentHandoff(Base):
    """A treatment moved from one doctor to another. Added, never changed."""

    __tablename__ = "treatment_handoffs"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        ForeignKeyConstraint(["clinic_id", "from_doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "to_doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "changed_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    treatment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    from_doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    to_doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reason: Mapped[str | None] = mapped_column(String(300))


class ToothFinding(Base):
    """A problem found on a tooth, or the tooth missing: the red and grey on the tooth chart.
    Resolved, never deleted: 'treated' when a finished treatment on the tooth fixed it,
    'cleared' when marked by hand as no longer a problem or entered by mistake."""

    __tablename__ = "tooth_findings"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "resolved_by_treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        ForeignKeyConstraint(["clinic_id", "noted_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "resolved_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tooth: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    problem: Mapped[str] = mapped_column(String(120), nullable=False)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    noted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    noted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolution: Mapped[str | None] = mapped_column(String(10))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_by_treatment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class MouthCheck(Base):
    """The "Full mouth check done" button: every tooth was looked at on this day."""

    __tablename__ = "mouth_checks"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "checked_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checked_on: Mapped[date] = mapped_column(Date, nullable=False)
    checked_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()


class Invoice(Base):
    """A printable invoice for items on the bill, numbered per financial year. Never deleted:
    a cancelled invoice keeps its number, and its items can go on a new one."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="invoices_clinic_id_id_key"),
        UniqueConstraint("clinic_id", "financial_year", "number", name="invoices_number_per_year"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "issued_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint("status IN ('issued', 'cancelled')", name="invoices_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    financial_year: Mapped[str] = mapped_column(String(7), nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    issued_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="issued")
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = created_at()


class InvoiceLine(Base):
    """One item on an invoice: a consultation fee or a treatment, never both."""

    __tablename__ = "invoice_lines"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "invoice_id"], ["invoices.clinic_id", "invoices.id"]),
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        CheckConstraint("(consultation_id IS NULL) <> (treatment_id IS NULL)", name="invoice_lines_one_item"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    treatment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    teeth: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Estimate(Base):
    """A printed quote with one or more options. Given, then accepted (its option's items
    become planned treatments) or declined. Expired is worked out from valid_until."""

    __tablename__ = "estimates"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="estimates_clinic_id_id_key"),
        UniqueConstraint("clinic_id", "financial_year", "number", name="estimates_number_per_year"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "doctor_id"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "decided_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint("status IN ('given', 'accepted', 'declined')", name="estimates_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    financial_year: Mapped[str] = mapped_column(String(7), nullable=False)
    given_on: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="given")
    discount_type: Mapped[str | None] = mapped_column(String(7))
    discount_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    accepted_option_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class EstimateOption(Base):
    __tablename__ = "estimate_options"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="estimate_options_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "estimate_id"], ["estimates.clinic_id", "estimates.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    estimate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EstimateItem(Base):
    __tablename__ = "estimate_items"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="estimate_items_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "option_id"], ["estimate_options.clinic_id", "estimate_options.id"]),
        ForeignKeyConstraint(["clinic_id", "service_id"], ["services.clinic_id", "services.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    option_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    teeth: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)
    sittings: Mapped[int | None] = mapped_column(SmallInteger)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ImportBatch(Base):
    """One patient import from an Excel or CSV file. It can be undone for 30 minutes, only
    while nothing has been recorded against its patients (the undo_patient_import function)."""

    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="import_batches_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "file_id"], ["files.clinic_id", "files.id"]),
        ForeignKeyConstraint(["clinic_id", "uploaded_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "undone_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False)
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False)
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    kept_numbers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at()
    undo_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class LedgerEntry(Base):
    """The patient's one combined bill, append only. Outstanding is the sum of amount_paise
    and may go negative (credit). Mistakes are corrected with new entries."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "consultation_id"], ["consultations.clinic_id", "consultations.id"]),
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    treatment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payment_mode: Mapped[str | None] = mapped_column(String(40))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()


class File(Base):
    """A stored file: X-ray, photo, document, logo or signature. The bytes live in file
    storage under the clinic's own folder; this row holds what they are. Deleting moves a
    file to a 30 day bin (deleted_at), it is only removed for good after purge_after."""

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="files_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "uploaded_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "deleted_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint("kind IN ('xray', 'photo', 'document', 'signature', 'logo', 'import')", name="files_kind"),
        CheckConstraint("size_bytes >= 0", name="files_size_not_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    thumbnail_key: Mapped[str | None] = mapped_column(String(300))
    teeth: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)
    consultation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    visit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lab_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str | None] = mapped_column(String(200))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(Base):
    """How a clinic pays for its plan. One per clinic. The plan in effect is clinics.plan;
    only payments and the platform change this row, so the API role can only read it."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("billing_cycle IN ('starter', 'monthly', 'yearly')", name="subscriptions_billing_cycle"),
        CheckConstraint("status IN ('active', 'past_due', 'cancelled')", name="subscriptions_status"),
        CheckConstraint("storage_addon_packs >= 0", name="subscriptions_packs_not_negative"),
        CheckConstraint("current_period_end > current_period_start", name="subscriptions_period_order"),
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), primary_key=True)
    billing_cycle: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    starter_pack_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    storage_addon_packs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String(40))
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


MESSAGE_KINDS_SQL = (
    "'visit_reminder', 'call_reminder', 'general', 'prescription', 'invoice', 'estimate', 'review_request', 'lab_ready'"
)


class MessageTemplate(Base):
    """A clinic's own wording for one kind of WhatsApp message in one language. Only changed
    wording is stored; the rest uses Denti's default (app.whatsapp.DEFAULTS)."""

    __tablename__ = "message_templates"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "updated_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint(f"kind IN ({MESSAGE_KINDS_SQL})", name="message_templates_kind"),
        CheckConstraint("language IN ('en', 'hi')", name="message_templates_language"),
        CheckConstraint("length(body) BETWEEN 1 AND 1000", name="message_templates_body_length"),
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    language: Mapped[str] = mapped_column(String(2), primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ShareLink(Base):
    """A link in a message that opens one document without signing in. Only the token's hash
    is kept. It stops working when it expires or someone at the clinic stops it."""

    __tablename__ = "share_links"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="share_links_clinic_id_id_key"),
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "prescription_id"], ["prescriptions.clinic_id", "prescriptions.id"]),
        ForeignKeyConstraint(["clinic_id", "invoice_id"], ["invoices.clinic_id", "invoices.id"]),
        ForeignKeyConstraint(["clinic_id", "estimate_id"], ["estimates.clinic_id", "estimates.id"]),
        ForeignKeyConstraint(["clinic_id", "created_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "revoked_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint(
            "(kind = 'prescription' AND prescription_id IS NOT NULL AND prescription_version IS NOT NULL"
            " AND invoice_id IS NULL AND estimate_id IS NULL)"
            " OR (kind = 'invoice' AND invoice_id IS NOT NULL AND prescription_id IS NULL AND estimate_id IS NULL)"
            " OR (kind = 'estimate' AND estimate_id IS NOT NULL AND prescription_id IS NULL AND invoice_id IS NULL)",
            name="share_links_one_document",
        ),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="share_links_revoked_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prescription_version: Mapped[int | None] = mapped_column(Integer)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageLog(Base):
    """Every WhatsApp message opened from Denti. Append only. Tap-to-send only knows the
    message was opened; the automatic version later records what happened to it."""

    __tablename__ = "message_log"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"]),
        ForeignKeyConstraint(["clinic_id", "share_link_id"], ["share_links.clinic_id", "share_links.id"]),
        ForeignKeyConstraint(["clinic_id", "follow_up_id"], ["follow_ups.clinic_id", "follow_ups.id"]),
        ForeignKeyConstraint(["clinic_id", "treatment_id"], ["treatments.clinic_id", "treatments.id"]),
        ForeignKeyConstraint(["clinic_id", "sent_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint(f"kind IN ({MESSAGE_KINDS_SQL})", name="message_log_kind"),
        CheckConstraint("language IN ('en', 'hi')", name="message_log_language"),
        CheckConstraint("method IN ('tap', 'api')", name="message_log_method"),
        CheckConstraint("status IN ('opened', 'sent', 'delivered', 'read', 'failed')", name="message_log_status"),
        CheckConstraint("method = 'api' OR sent_by IS NOT NULL", name="message_log_tap_has_sender"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="tap")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="opened")
    to_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    share_link_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    follow_up_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    treatment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    sent_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at()


class PlanChange(Base):
    """A move to another plan. Moving up applies once paid; moving down waits for the end of
    the paid period. Only apply_plan_change (a database function) changes clinics.plan."""

    __tablename__ = "plan_changes"
    __table_args__ = (
        ForeignKeyConstraint(["clinic_id", "requested_by"], ["staff.clinic_id", "staff.id"]),
        ForeignKeyConstraint(["clinic_id", "cancelled_by"], ["staff.clinic_id", "staff.id"]),
        CheckConstraint("kind IN ('upgrade', 'downgrade')", name="plan_changes_kind"),
        CheckConstraint("status IN ('pending', 'applied', 'cancelled')", name="plan_changes_status"),
        CheckConstraint(
            "from_plan IN ('basic', 'standard', 'pro') AND to_plan IN ('basic', 'standard', 'pro')", name="plan_changes_plans"
        ),
        CheckConstraint("from_plan <> to_plan", name="plan_changes_different"),
        CheckConstraint(
            "amount_paise >= 0 AND gst_paise >= 0 AND total_paise = amount_paise + gst_paise", name="plan_changes_amounts"
        ),
        CheckConstraint("kind = 'upgrade' OR total_paise = 0", name="plan_changes_only_upgrades_charge"),
        CheckConstraint("(status = 'applied') = (applied_at IS NOT NULL)", name="plan_changes_applied_at"),
        CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="plan_changes_cancelled_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    from_plan: Mapped[str] = mapped_column(String(20), nullable=False)
    to_plan: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    payment_method: Mapped[str | None] = mapped_column(String(20))
    payment_ref: Mapped[str | None] = mapped_column(String(100))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    keep_staff_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
