from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import SessionLocal, Base, engine
from schemas import Measurement
from schemas_prediction import ConsumptionResponse
import crud
from models import NotificationModel
from consumption import calculate_consumption
from agent import call_hoplite_agent, AgentError

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/measurements")
def add_measurement(m: Measurement):
    db = SessionLocal()
    try:
        measurement = crud.create_measurement(db, m)
        return {
            "status": "stored",
            "id": measurement.id,
            "device_id": measurement.device_id,
        }
    finally:
        db.close()


@app.get("/measurements")
def get_measurements():
    db = SessionLocal()
    try:
        measurements = crud.get_measurements(db)
        return [
            {
                "id": m.id,
                "device_id": m.device_id,
                "weight": m.weight,
                "battery": m.battery,
                "timestamp": m.timestamp,
            }
            for m in measurements
        ]
    finally:
        db.close()


def _latest_notification(db, device_id: str, notification_type: str):
    return (
        db.query(NotificationModel)
        .filter(
            NotificationModel.device_id == device_id,
            NotificationModel.notification_type == notification_type,
        )
        .order_by(NotificationModel.sent_at.desc())
        .first()
    )


@app.get("/consumption", response_model=ConsumptionResponse)
def get_consumption(device_id: str = "Mysterious soldier"):
    db = SessionLocal()
    try:
        measurements = crud.get_measurements(db, device_id=device_id)
        prediction = calculate_consumption(measurements)

        notification = None
        if prediction.days_remaining is not None and prediction.days_remaining <= 7:
            notification_type = (
                "URGENT_REPLENISHMENT_NOTIFICATION"
                if prediction.days_remaining <= 3
                else "LOW_STOCK_NOTIFICATION"
            )

            previous = _latest_notification(db, device_id, notification_type)
            if previous is None:
                backend_payload = {
                    "device_id": prediction.device_id,
                    "current_weight_g": prediction.current_weight_g,
                    "consumption_rate_g_day": prediction.consumption_rate_g_day,
                    "days_remaining": prediction.days_remaining,
                    "estimated_runout_at": prediction.estimated_runout_at,
                    "confidence": prediction.confidence,
                    "notification_state": notification_type,
                    "amazon_url": None,
                }
                try:
                    notification = call_hoplite_agent(backend_payload)
                except AgentError as exc:
                    notification = {
                        "action": notification_type,
                        "priority": "high" if notification_type == "URGENT_REPLENISHMENT_NOTIFICATION" else "normal",
                        "message": "Your product is running low. Please plan to replenish it soon.",
                        "amazon_option": None,
                        "agent_error": str(exc),
                    }

                if notification:
                    record = NotificationModel(
                        device_id=device_id,
                        notification_type=notification.get("action", notification_type),
                        priority=notification.get("priority", "normal"),
                        message=notification.get("message", ""),
                        amazon_option=notification.get("amazon_option"),
                        status="generated",
                    )
                    db.add(record)
                    db.commit()

        return ConsumptionResponse(
            device_id=prediction.device_id,
            current_weight_g=prediction.current_weight_g,
            consumption_rate_g_day=prediction.consumption_rate_g_day,
            days_remaining=prediction.days_remaining,
            estimated_runout_at=prediction.estimated_runout_at,
            confidence=prediction.confidence,
            sample_count=prediction.sample_count,
            data_span_days=prediction.data_span_days,
            notification=notification,
        )
    finally:
        db.close()


@app.get("/notifications")
def get_notifications(device_id: str = "Mysterious soldier"):
    db = SessionLocal()
    try:
        rows = (
            db.query(NotificationModel)
            .filter(NotificationModel.device_id == device_id)
            .order_by(NotificationModel.sent_at.desc())
            .all()
        )
        return [
            {
                "id": row.id,
                "device_id": row.device_id,
                "notification_type": row.notification_type,
                "priority": row.priority,
                "message": row.message,
                "amazon_option": row.amazon_option,
                "sent_at": row.sent_at,
                "status": row.status,
            }
            for row in rows
        ]
    finally:
        db.close()


@app.get("/")
def root():
    return {
        "service": "Hoplite API",
        "status": "running",
    }
