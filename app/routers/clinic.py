from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.audit import record
from app.clinic_settings import get_settings
from app.deps import Current, get_current, require_admin
from app.documents import BRANDED_PLANS, doctor_for, letterhead_for, pdf_response
from app.files_service import read_file, store_file
from app.logo import LogoError, clean_logo
from app.models import ClinicSettings, File
from app.pdf import letterhead_preview_pdf
from app.schemas import ClinicDetailsIn, ClinicDetailsOut, LogoIn
from app.security import now

# The clinic's details and letterhead: its logo, tagline, address, phone, email, website and
# GSTIN, printed on prescriptions, invoices and estimates, and the logo in the app's top bar.
# Everyone signed in may read them (the top bar shows the logo to all); the owner and the
# receptionist change them. Brand colour and designs come with Standard and Pro, and hiding
# "Powered by Denti" with Pro.

BIN_DAYS = 30
DETAIL_FIELDS = (
    "tagline", "address", "phone", "email", "website", "gstin", "brand_colour", "letterhead_design",
    "footer_text", "hide_powered_by", "preprinted_letterhead", "preprinted_top_margin_mm",
)

router = APIRouter(prefix="/clinic", tags=["clinic details"])


def _logo(db, settings: ClinicSettings) -> File | None:
    if settings.logo_file_id is None:
        return None
    stored = db.get(File, settings.logo_file_id)
    return stored if stored is not None and stored.deleted_at is None else None


def _details_out(current: Current) -> ClinicDetailsOut:
    db, clinic = current.db, current.clinic
    settings = get_settings(db, clinic)
    logo = _logo(db, settings)
    return ClinicDetailsOut(
        name=clinic.name,
        plan=clinic.plan,
        **{name: getattr(settings, name) for name in DETAIL_FIELDS},
        has_logo=logo is not None,
        logo_version=str(logo.id) if logo else None,
        can_brand=clinic.plan in BRANDED_PLANS,
        can_hide_powered_by=clinic.plan == "pro",
    )


@router.get("/details", response_model=ClinicDetailsOut)
def clinic_details(current: Current = Depends(get_current)):
    details = _details_out(current)
    current.db.commit()  # the settings row may have just been created with its defaults
    return details


@router.put("/details", response_model=ClinicDetailsOut)
def save_clinic_details(payload: ClinicDetailsIn, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    if clinic.plan not in BRANDED_PLANS and (payload.brand_colour or payload.letterhead_design != "classic"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Letterhead designs and brand colour come with the Standard and Pro plans.",
        )
    if clinic.plan != "pro" and payload.hide_powered_by:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail='Removing "Powered by Denti" from documents comes with the Pro plan.'
        )
    settings = get_settings(db, clinic)
    changes = {}
    for name in DETAIL_FIELDS:
        value = getattr(payload, name)
        if getattr(settings, name) != value:
            changes[name] = value
            setattr(settings, name, value)
    if changes:
        settings.updated_at = now()
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="clinic",
            entity_id=clinic.id, changes={"details": changes}, request=request,
        )
    db.commit()
    return _details_out(current)


def _bin_logo(current: Current, settings: ClinicSettings) -> bool:
    """Moves the current logo to the bin, where it stays for 30 days. True if there was one."""
    old = _logo(current.db, settings)
    settings.logo_file_id = None
    if old is None:
        return False
    old.deleted_at = now()
    old.deleted_by = current.staff.id
    old.purge_after = old.deleted_at + timedelta(days=BIN_DAYS)
    return True


@router.put("/logo", response_model=ClinicDetailsOut)
def set_logo(payload: LogoIn, request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    try:
        png, width, height = clean_logo(payload.image_data)
    except LogoError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))
    settings = get_settings(db, clinic)
    had_one = _bin_logo(current, settings)
    stored = store_file(
        db, clinic_id=clinic.id, kind="logo", data=png, content_type="image/png",
        uploaded_by=current.staff.id, width=width, height=height,
    )
    settings.logo_file_id = stored.id
    settings.updated_at = now()
    record(
        db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="clinic",
        entity_id=clinic.id, changes={"logo": "replaced" if had_one else "added"}, request=request,
    )
    db.commit()
    return _details_out(current)


@router.delete("/logo", response_model=ClinicDetailsOut)
def remove_logo(request: Request, current: Current = Depends(require_admin)):
    db, clinic = current.db, current.clinic
    settings = get_settings(db, clinic)
    if _bin_logo(current, settings):
        settings.updated_at = now()
        record(
            db, clinic_id=clinic.id, action="update", actor_id=current.staff.id, entity_type="clinic",
            entity_id=clinic.id, changes={"logo": "removed"}, request=request,
        )
    db.commit()
    return _details_out(current)


@router.get("/logo")
def get_logo(current: Current = Depends(get_current)):
    logo = _logo(current.db, get_settings(current.db, current.clinic))
    if logo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo has been added yet.")
    return Response(content=read_file(logo), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/letterhead/preview")
def preview_letterhead(current: Current = Depends(require_admin)):
    """A sample page with the letterhead as saved, before printing real documents. When the
    person looking treats patients, their own name shows where a prescription's doctor goes."""
    staff = current.staff
    doctor = doctor_for(current.db, staff) if staff.treats_patients else None
    content = letterhead_preview_pdf(letterhead_for(current.db, current.clinic), doctor)
    return pdf_response(content, "Letterhead sample.pdf")
