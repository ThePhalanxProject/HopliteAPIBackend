from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional


@dataclass
class ConsumptionPrediction:
    device_id: str
    current_weight_g: float
    consumption_rate_g_day: float
    days_remaining: Optional[float]
    estimated_runout_at: Optional[datetime]
    confidence: float
    sample_count: int
    data_span_days: float


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def calculate_consumption(measurements: List[object], empty_threshold_g: float = 50.0) -> ConsumptionPrediction:
    """Estimate daily net consumption over the observed measurement history."""
    if not measurements:
        raise ValueError("No measurements available")

    points = sorted(
        [m for m in measurements if m.timestamp is not None and m.weight is not None],
        key=lambda m: _to_utc(m.timestamp),
    )
    if not points:
        raise ValueError("No valid measurements available")

    latest = points[-1]
    latest_time = _to_utc(latest.timestamp)
    span_days = (latest_time - _to_utc(points[0].timestamp)).total_seconds() / 86400
    has_history = len(points) >= 3 and span_days > 0
    consumption_rate = (
        max(0.0, (float(points[0].weight) - float(latest.weight)) / span_days)
        if has_history
        else 0.0
    )

    if consumption_rate <= 0 or float(latest.weight) <= empty_threshold_g:
        days_remaining = 0.0 if float(latest.weight) <= empty_threshold_g else None
        runout = latest_time if days_remaining == 0.0 else None
    else:
        usable_weight = max(0.0, float(latest.weight) - empty_threshold_g)
        days_remaining = usable_weight / consumption_rate
        runout = latest_time + timedelta(days=days_remaining)

    return ConsumptionPrediction(
        device_id=latest.device_id,
        current_weight_g=round(float(latest.weight), 2),
        consumption_rate_g_day=round(consumption_rate, 2),
        days_remaining=round(days_remaining, 1) if days_remaining is not None else None,
        estimated_runout_at=runout,
        confidence=1.0 if has_history else 0.0,
        sample_count=len(points),
        data_span_days=span_days,
    )
