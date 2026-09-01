from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Record
from ..schemas import RecordCreate, RecordRead

router = APIRouter(prefix="/records", tags=["records"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/", response_model=RecordRead)
def create_record(payload: RecordCreate, db: Session = Depends(get_db)) -> RecordRead:
    record = Record(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/", response_model=list[RecordRead])
def list_records(db: Session = Depends(get_db)) -> list[RecordRead]:
    return db.query(Record).all()


@router.get("/{record_id}", response_model=RecordRead)
def get_record(record_id: int, db: Session = Depends(get_db)) -> RecordRead:
    record = db.query(Record).filter(Record.id == record_id).first()
    if record is None:
        raise LookupError("Record not found")
    return record
