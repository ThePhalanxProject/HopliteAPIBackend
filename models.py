from sqlalchemy import Column, Integer, Float, String, DateTime
from datetime import datetime

from database import Base


class MeasurementModel(Base):
    __tablename__ = "measurements"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String)
    weight = Column(Float)
    battery = Column(Float)
    timestamp = Column(DateTime, default=datetime.utcnow)
