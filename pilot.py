from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from consumption import _to_utc
from pilot_oracle import OracleMessage, PriceSearch, pilot_prices, stock_message
from database import Base, SessionLocal, engine
from models import PilotMessagePreference, PilotPack, PilotSession, PilotUsageEvent, PilotUser

STARTING_WEIGHT_G = 1500
DOSE_G = 65

router = APIRouter(prefix="/pilot", tags=["pilot"])
bearer = HTTPBearer(auto_error=False)


class UsageTap(BaseModel):
    request_id: UUID


class PackStatus(BaseModel):
    started_at: datetime | None
    current_weight_g: int
    full_doses_remaining: int
    sample_count: int
    user_points: int
    data_span_days: float
    consumption_rate_g_day: float | None = None
    days_remaining: float | None = None
    estimated_runout_at: datetime | None = None


class UsageResult(PackStatus):
    points_earned: int
    oracle: OracleMessage


def get_db():
    with SessionLocal() as db:
        yield db


def token_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def participant(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> PilotUser:
    if credentials is not None:
        digest = token_digest(credentials.credentials)
        user = db.scalar(select(PilotUser).where(PilotUser.token_hash == digest))
        if user is None:
            user = db.scalar(select(PilotUser).join(PilotSession).where(
                PilotSession.token_hash == digest,
                PilotSession.expires_at > datetime.now(timezone.utc),
            ))
        if user is not None:
            return user
    raise HTTPException(401, "Invalid pilot credential", headers={"WWW-Authenticate": "Bearer"})


class MessagePreference(BaseModel):
    language: Literal["en", "fr"]


def message_language(db: Session, user_id: int) -> str:
    preference = db.get(PilotMessagePreference, user_id)
    return preference.language if preference else "en"


@router.get("/preferences/message-language", response_model=MessagePreference)
def read_message_language(
    user: PilotUser = Depends(participant), db: Session = Depends(get_db)
):
    return MessagePreference(language=message_language(db, user.id))


@router.put("/preferences/message-language", response_model=MessagePreference)
def set_message_language(
    preference: MessagePreference,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
):
    db.scalar(select(PilotUser.id).where(PilotUser.id == user.id).with_for_update())
    db.merge(PilotMessagePreference(user_id=user.id, language=preference.language))
    db.commit()
    return preference


def get_pack(db: Session, user: PilotUser, *, lock: bool = False) -> PilotPack:
    query = select(PilotPack).where(PilotPack.user_id == user.id)
    pack = db.scalar(query.with_for_update() if lock else query)
    if pack is None:
        raise HTTPException(404, "No pilot pack assigned")
    return pack


def pack_status(db: Session, pack: PilotPack, now: datetime) -> PackStatus:
    count, grams_used, last_use = db.execute(
        select(
            func.count(PilotUsageEvent.id),
            func.coalesce(func.sum(PilotUsageEvent.grams_used), 0),
            func.max(PilotUsageEvent.occurred_at),
        ).where(PilotUsageEvent.pack_id == pack.id)
    ).one()
    remaining = STARTING_WEIGHT_G - grams_used
    started_at = _to_utc(pack.started_at) if pack.started_at is not None else None
    elapsed_days = (now - started_at).total_seconds() / 86400 if started_at else 0.0
    result = PackStatus(
        started_at=started_at,
        current_weight_g=remaining,
        full_doses_remaining=remaining // DOSE_G,
        sample_count=count,
        user_points=count,
        data_span_days=elapsed_days,
    )
    if remaining == 0:
        result.days_remaining = 0.0
        result.estimated_runout_at = _to_utc(last_use)
    if count >= 2 and elapsed_days >= 2.0:
        rate = grams_used / elapsed_days
        result.consumption_rate_g_day = rate
        if remaining > 0:
            result.days_remaining = remaining / rate
            result.estimated_runout_at = now + timedelta(days=result.days_remaining)
    return result


@router.get("/pack", response_model=PackStatus)
def read_pack(user: PilotUser = Depends(participant), db: Session = Depends(get_db)):
    return pack_status(db, get_pack(db, user), datetime.now(timezone.utc))


@router.post("/pack/start", response_model=PackStatus)
def start_pack(user: PilotUser = Depends(participant), db: Session = Depends(get_db)):
    pack = get_pack(db, user, lock=True)
    now = datetime.now(timezone.utc)
    if pack.started_at is None:
        pack.started_at = now
    result = pack_status(db, pack, now)
    db.commit()
    return result


@router.post("/pack/usage", response_model=UsageResult)
def record_usage(
    tap: UsageTap,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
):
    pack = get_pack(db, user, lock=True)
    if pack.started_at is None:
        raise HTTPException(409, "Start the pilot pack before logging usage")

    request_id = str(tap.request_id)
    duplicate = db.scalar(select(PilotUsageEvent.id).where(
        PilotUsageEvent.pack_id == pack.id,
        PilotUsageEvent.request_id == request_id,
    ))
    now = datetime.now(timezone.utc)
    result = pack_status(db, pack, now)
    if duplicate is None:
        if result.current_weight_g == 0:
            raise HTTPException(409, "The pilot pack is empty")
        db.add(PilotUsageEvent(
            pack_id=pack.id,
            request_id=request_id,
            grams_used=min(DOSE_G, result.current_weight_g),
            occurred_at=now,
        ))
        db.flush()
        result = pack_status(db, pack, now)
    language = language or message_language(db, user.id)
    db.commit()

    points_earned = int(duplicate is None)
    oracle = stock_message(
        result, language, points_earned=points_earned, generate=bool(points_earned)
    )
    return UsageResult(
        **result.model_dump(), points_earned=points_earned, oracle=oracle
    )


@router.get("/pack/oracle", response_model=OracleMessage)
def read_oracle(
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
):
    status = pack_status(db, get_pack(db, user), datetime.now(timezone.utc))
    language = language or message_language(db, user.id)
    db.rollback()
    return stock_message(status, language)


@router.post("/prices")
def search_prices(
    query: PriceSearch,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
):
    language = query.language or message_language(db, user.id)
    db.rollback()
    return pilot_prices(query.brand, language, query.city, query.country)


def provision_participant(db: Session, participant_code: str) -> str:
    token = token_urlsafe(32)
    user = PilotUser(participant_code=participant_code, token_hash=token_digest(token))
    db.add(user)
    db.flush()
    db.add(PilotPack(user_id=user.id))
    db.commit()
    return token


if __name__ == "__main__":
    parser = ArgumentParser(description="Provision a Hoplite pilot participant and pack")
    parser.add_argument("participant_code")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    try:
        with SessionLocal() as db:
            print(provision_participant(db, args.participant_code))
    except IntegrityError:
        parser.exit(1, "Could not provision the participant. Use a unique participant code.\n")
