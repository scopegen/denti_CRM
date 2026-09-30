"""The tooth chart, worked out from the records and never stored as colours.

A tooth is, in this order of importance:
- planned (amber): a planned or ongoing treatment covers it;
- problem (red): a problem found on it is not resolved;
- missing (grey): it is marked missing;
- done (green): a finished treatment covers it;
- healthy (white): nothing recorded.

Finishing a treatment resolves the problems on its teeth. Finishing one that removes the
tooth (an extraction) marks the tooth missing instead.
"""
import uuid
from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Clinic, Picklist, Service, ToothFinding, Treatment
from app.security import now
from app.teeth import check_teeth

MISSING = "missing"
_ORDER = ["planned", "problem", "missing", "done"]


def require_chart_plan(clinic: Clinic) -> None:
    if clinic.plan == "basic":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The tooth chart comes with the Standard and Pro plans.")


def statuses(findings: Iterable[ToothFinding], treatments: Iterable[Treatment]) -> dict[int, str]:
    """Each tooth with something recorded, and its colour. Healthy teeth are left out."""
    marks: dict[int, set[str]] = {}
    for f in findings:
        if f.resolution is None:
            marks.setdefault(f.tooth, set()).add("missing" if f.problem == MISSING else "problem")
    for t in treatments:
        if t.status in ("planned", "ongoing"):
            kind = "planned"
        elif t.status == "finished":
            kind = "done"
        else:
            continue
        for tooth in t.teeth:
            marks.setdefault(tooth, set()).add(kind)
    return {tooth: next(s for s in _ORDER if s in found) for tooth, found in marks.items()}


def tooth_problems(db: Session) -> list[str]:
    return list(
        db.scalars(
            select(Picklist.value)
            .where(Picklist.list == "tooth_problem", Picklist.active.is_(True))
            .order_by(Picklist.sort_order, Picklist.value)
        )
    )


def _open_findings(db: Session, patient_id: uuid.UUID, teeth: Iterable[int]) -> list[ToothFinding]:
    return list(
        db.scalars(
            select(ToothFinding).where(
                ToothFinding.patient_id == patient_id, ToothFinding.tooth.in_(list(teeth)), ToothFinding.resolution.is_(None)
            )
        )
    )


def _resolve(finding: ToothFinding, how: str, actor: uuid.UUID, treatment_id: uuid.UUID | None = None) -> None:
    finding.resolution = how
    finding.resolved_at = now()
    finding.resolved_by = actor
    finding.resolved_by_treatment_id = treatment_id


def add_finding(
    db: Session,
    clinic: Clinic,
    *,
    patient_id: uuid.UUID,
    tooth: int,
    problem: str,
    actor: uuid.UUID,
    consultation_id: uuid.UUID | None = None,
) -> ToothFinding:
    """Marks a problem on a tooth, or the tooth missing. Marking the same problem twice keeps
    the first. A missing tooth has no other problems, so marking it missing clears them."""
    try:
        check_teeth([tooth])
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))
    problem = problem.strip()
    if problem != MISSING and problem not in tooth_problems(db):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a problem from the clinic's list.")
    open_ones = _open_findings(db, patient_id, [tooth])
    for existing in open_ones:
        if existing.problem == problem:
            return existing
    if problem == MISSING:
        for existing in open_ones:
            _resolve(existing, "cleared", actor)
    elif any(f.problem == MISSING for f in open_ones):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tooth {tooth} is marked missing. Mark it present first.",
        )
    finding = ToothFinding(
        clinic_id=clinic.id, patient_id=patient_id, tooth=tooth, problem=problem, consultation_id=consultation_id,
        noted_by=actor,
    )
    db.add(finding)
    db.flush()
    return finding


def clear_finding(finding: ToothFinding, actor: uuid.UUID) -> None:
    if finding.resolution is None:
        _resolve(finding, "cleared", actor)


def after_treatment_finished(db: Session, clinic: Clinic, t: Treatment, actor: uuid.UUID) -> None:
    """A finished treatment fixes the problems on its teeth; an extraction leaves them missing."""
    if not t.teeth:
        return
    for finding in _open_findings(db, t.patient_id, t.teeth):
        _resolve(finding, "treated", actor, treatment_id=t.id)
    if db.get(Service, t.service_id).removes_tooth:
        for tooth in t.teeth:
            db.add(
                ToothFinding(clinic_id=clinic.id, patient_id=t.patient_id, tooth=tooth, problem=MISSING, noted_by=actor)
            )
        db.flush()
