from sqlalchemy.orm import Session
from datetime import datetime

from models import MeasurementModel
from schemas import Measurement


def create_measurement(db: Session, measurement: Measurement):
    new_entry = MeasurementModel(
        device_id=measurement.device_id,
        weight=measurement.weight,
        battery=measurement.battery_percentage,
        timestamp=datetime.utcnow(),
    )

    db.add(new_entry)
    db.commit()
    db.refresh(new_entry)
    return new_entry


def get_measurements(db: Session, device_id: str | None = None):
    query = db.query(MeasurementModel)
    if device_id is not None:
        query = query.filter(MeasurementModel.device_id == device_id)

    return query.order_by(MeasurementModel.timestamp.desc()).all()
