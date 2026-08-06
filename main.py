from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
#SQL database module
from datetime import datetime
from database import SessionLocal, Base

app = FastAPI()
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # OK during development   
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from schemas import Measurement
import crud
# Data model coming from Arduino
from database import engine

Base.metadata.create_all(bind=engine)

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


#End point code
@app.post("/measurements")
def add_measurement(m: Measurement):

    db = SessionLocal()

    try:

        measurement = crud.create_measurement(db, m)

        return {
            "status": "stored",
            "id": measurement.id,
            "device_id": measurement.device_id
        }

    finally:

        db.close()

#Get code
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
                "timestamp": m.timestamp
            }
            for m in measurements
        ]

    finally:

        db.close()

@app.get("/")
def root():
    return {
        "service": "Hoplite API",
        "status": "running"
    }
