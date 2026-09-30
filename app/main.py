from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.routers import (
    audit_log,
    auth,
    billing,
    consultations,
    estimates,
    imports,
    invoices,
    patient_files,
    patients,
    picklists,
    plan,
    services,
    shared_documents,
    staff,
    today,
    tooth_chart,
    treatments,
    whatsapp,
)

app = FastAPI(title="Denti API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the browser read the file name of downloaded PDFs.
    expose_headers=["Content-Disposition"],
)

app.include_router(auth.router)
app.include_router(staff.router)
app.include_router(audit_log.router)
app.include_router(patients.router)
app.include_router(picklists.router)
app.include_router(today.router)
app.include_router(services.router)
app.include_router(consultations.router)
app.include_router(plan.router)
app.include_router(patient_files.router)
app.include_router(treatments.router)
app.include_router(tooth_chart.router)
app.include_router(billing.router)
app.include_router(invoices.router)
app.include_router(estimates.router)
app.include_router(imports.router)
app.include_router(whatsapp.router)
app.include_router(shared_documents.router)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("select 1"))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})
    return {"status": "ok", "database": "ok"}
