from sqlalchemy.orm import Session
from datetime import datetime

from models import MeasurementModel
from schemas import Measurement


def create_measurement(db: Session, measurement: Measurement):

    new_entry = MeasurementModel(
        device_id=measurement.device_id,
        weight=measurement.weight,
        battery=measurement.battery_percentage,
        timestamp=datetime.utcnow()
    )

    db.add(new_entry)
    db.commit()
    db.refresh(new_entry)

    return new_entry


def get_measurements(db: Session):

    return (
        db.query(MeasurementModel)
        .order_by(MeasurementModel.timestamp.desc())
        .all()
    )
