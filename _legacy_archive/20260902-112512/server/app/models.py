from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.sql import func

from .database import Base


class Record(Base):
    __tablename__ = "records"

    id = Column(Integer, primary_key=True, index=True)
    doctor_name = Column(String, nullable=False, index=True)
    service = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    category = Column(String, nullable=False)
    date = Column(DateTime, server_default=func.now())
