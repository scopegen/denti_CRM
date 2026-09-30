from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.deps import Current, get_current
from app.models import Picklist

# The lists each clinic edits, read by the forms (medical conditions, cities and so on).

router = APIRouter(prefix="/picklists", tags=["picklists"])

ListName = Literal["medical_condition", "tooth_problem", "payment_mode", "city", "lab_material"]


@router.get("/{list_name}", response_model=list[str])
def get_picklist(list_name: ListName, current: Current = Depends(get_current)):
    return current.db.scalars(
        select(Picklist.value)
        .where(Picklist.list == list_name, Picklist.active.is_(True))
        .order_by(Picklist.sort_order, Picklist.value)
    ).all()
