from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
#SQL database module
from sqlalchemy import Column, Integer, Float, String, DateTime
from database import Base
from datetime import datetime
from database import SessionLocal
from sqlalchemy import Column, Integer, Float, String, DateTime
from database import Base

app = FastAPI()
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # OK during development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MeasurementModel(Base):
    __tablename__ = "measurements"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String)
    weight = Column(Float)
    battery = Column(Float)
    timestamp = Column(DateTime)

# Data model coming from Arduino
from pydantic import BaseModel

class Measurement(BaseModel):
    weight: float
    battery_percentage: float
    device_id: str | None = "Mysterious soldier"

@app.get("/health")
def health():
    return {"status": "ok"}

#@app.post("/measurements")
#Test only code
#___________________________________________________
#def add_measurement(m: Measurement):
#    print("NEW DATA:", m)

#    return {
#        "status": "stored",
#        "device_id": m.device_id,
#        "weight": m.weight,
#        "battery_level": m.battery_percentage,
#        "timestamp": datetime.utcnow()
#    }
#______________________________________________________
#New database insertion code



@app.post("/measurements")
def add_measurement(m: Measurement):
    print("NEW DATA:", m)

    db = SessionLocal()

    new_entry = MeasurementModel(
        device_id=m.device_id,
        weight=m.weight,
        battery=m.battery_percentage,
        timestamp=datetime.utcnow()
    )

    db.add(new_entry)
    db.commit()
    db.close()

    return {
        "status": "stored",
        "device_id": m.device_id
    }


@app.get("/measurements")
def get_measurements():
    db = SessionLocal()

    measurements = db.query(MeasurementModel).all()

    result = []

    for m in measurements:
        result.append({
            "id": m.id,
            "device_id": m.device_id,
            "weight": m.weight,
            "battery": m.battery,
            "timestamp": m.timestamp
        })

    db.close()

    return result
