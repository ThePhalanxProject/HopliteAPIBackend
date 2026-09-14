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
    """Estimate daily consumption using the latest three readings as three consecutive days."""
    if not measurements:
        raise ValueError("No measurements available")

    points = sorted(
        [m for m in measurements if m.timestamp is not None and m.weight is not None],
        key=lambda m: _to_utc(m.timestamp),
    )
    if not points:
        raise ValueError("No valid measurements available")

    # For the MVP, ignore older/test history and use only the latest three readings.
    points = points[-3:]
    latest = points[-1]
    latest_time = _to_utc(latest.timestamp)

    # Three readings are treated as three consecutive daily readings. Their actual
    # timestamps are deliberately ignored when calculating the consumption rate.
    if len(points) < 3:
        return ConsumptionPrediction(
            device_id=latest.device_id,
            current_weight_g=float(latest.weight),
            consumption_rate_g_day=0.0,
            days_remaining=None,
            estimated_runout_at=None,
            confidence=0.0,
            sample_count=len(points),
            data_span_days=0.0,
        )

    # Reading 1 = day 0, reading 2 = day 1, reading 3 = day 2.
    # This makes the rate independent of the device/server timestamps.
    x = [0.0, 1.0, 2.0]
    y = [float(m.weight) for m in points]
    x_mean = sum(x) / len(x)
    y_mean = sum(y) / len(y)
    denominator = sum((value - x_mean) ** 2 for value in x)
    slope = (
        sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(len(points))) / denominator
        if denominator
        else 0.0
    )
    consumption_rate = max(0.0, -slope)

    # The three readings represent a 2-day elapsed period (day 0 -> day 2).
    assumed_span_days = 2.0

    if consumption_rate <= 0 or float(latest.weight) <= empty_threshold_g:
        days_remaining = 0.0 if float(latest.weight) <= empty_threshold_g else None
        runout = latest_time if days_remaining == 0.0 else None
    else:
        usable_weight = max(0.0, float(latest.weight) - empty_threshold_g)
        days_remaining = usable_weight / consumption_rate
        runout = latest_time + timedelta(days=days_remaining)

    # Confidence is based on having the full three-reading window. It no longer
    # depends on the real timestamps of those readings.
    confidence = 1.0

    return ConsumptionPrediction(
        device_id=latest.device_id,
        current_weight_g=round(float(latest.weight), 2),
        consumption_rate_g_day=round(consumption_rate, 2),
        days_remaining=round(days_remaining, 1) if days_remaining is not None else None,
        estimated_runout_at=runout,
        confidence=confidence,
        sample_count=len(points),
        data_span_days=assumed_span_days,
    )
