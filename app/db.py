import uuid
from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# pool_pre_ping checks a pooled connection before using it, because Neon closes idle
# connections when its compute scales to zero.
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


_SET_CLINIC = text("select set_config('app.clinic_id', :clinic_id, true)")


def use_clinic(db: Session, clinic_id: uuid.UUID) -> None:
    """Scope this session to one clinic. Row level security then hides every other
    clinic's rows. The setting is transaction local, so it is applied again at the start
    of every transaction (see _apply_clinic below)."""
    db.info["clinic_id"] = str(clinic_id)
    if db.in_transaction():
        db.execute(_SET_CLINIC, {"clinic_id": str(clinic_id)})


@event.listens_for(SessionLocal, "after_begin")
def _apply_clinic(session: Session, _transaction, connection) -> None:
    clinic_id = session.info.get("clinic_id")
    if clinic_id:
        connection.execute(_SET_CLINIC, {"clinic_id": clinic_id})


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
