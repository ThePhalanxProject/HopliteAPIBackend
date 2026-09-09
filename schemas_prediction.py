from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ConsumptionResponse(BaseModel):
    device_id: str
    current_weight_g: float
    consumption_rate_g_day: float
    days_remaining: Optional[float]
    estimated_runout_at: Optional[datetime]
    confidence: float
    sample_count: int
    data_span_days: float
    notification: Optional[dict] = None
