from pydantic import BaseModel
from datetime import datetime


class Measurement(BaseModel):
    weight: float
    battery_percentage: float
    device_id: str = "Mysterious soldier"


class MeasurementResponse(BaseModel):
    id: int
    device_id: str
    weight: float
    battery: float
    timestamp: datetime
