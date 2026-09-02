from datetime import datetime

from pydantic import BaseModel, Field


class RecordCreate(BaseModel):
    doctor_name: str
    service: str
    amount: float
    category: str


class RecordRead(RecordCreate):
    id: int
    date: datetime


class ExportRequest(BaseModel):
    from_date: str
    to_date: str
    physicians: list[str] = Field(default_factory=list)
    skip_analyzer: bool = True


class ExportResponse(BaseModel):
    status: str
    pid: int | None = None
    log_file: str


class LogResponse(BaseModel):
    log_file: str
    content: str
