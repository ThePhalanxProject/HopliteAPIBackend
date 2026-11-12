from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, computed_field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from consumption import _to_utc
from pilot_oracle import OracleMessage, PriceSearch, pilot_prices, stock_message
from database import Base, SessionLocal, engine
from models import PilotMessagePreference, PilotPack, PilotRefillEvent, PilotSession, PilotUsageEvent, PilotUser

CAPACITY_ML = 1500
DOSE_ML = 65
USAGE_POINTS = 5
REFILL_POINTS = 10
POINTS_PER_EURO = 50

router = APIRouter(prefix="/pilot", tags=["pilot"])
bearer = HTTPBearer(auto_error=False)


class UsageTap(BaseModel):
    request_id: UUID


class PackStatus(BaseModel):
    started_at: datetime | None
    last_refill_at: datetime | None = None
    current_volume_l: float
    capacity_l: float
    dose_ml: int
    full_doses_remaining: int
    sample_count: int
    user_points: int
    cashback_earned_cents: int
    points_to_next_cashback: int
    data_span_days: float
    consumption_rate_l_day: float | None = None
    days_remaining: float | None = None
    estimated_runout_at: datetime | None = None

    @computed_field
    @property
    def stock_status(self) -> Literal["not_started", "ok", "low", "urgent", "empty"]:
        if self.started_at is None:
            return "not_started"
        if self.current_volume_l == 0:
            return "empty"
        if self.full_doses_remaining == 0 or (
            self.days_remaining is not None and self.days_remaining <= 3
        ):
            return "urgent"
        if self.days_remaining is not None and self.days_remaining <= 7:
            return "low"
        return "ok"

    @computed_field
    @property
    def refill_needed(self) -> bool:
        return self.stock_status in {"low", "urgent", "empty"}


class UsageResult(PackStatus):
    points_earned: int
    refill_recorded: bool
    cashback_unlocked_cents: int
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
    refills = select(
        func.count(PilotRefillEvent.id).label("refill_count"),
        func.max(PilotRefillEvent.after_usage_id).label("last_refill_usage"),
        func.max(PilotRefillEvent.occurred_at).label("last_refill_at"),
    ).where(PilotRefillEvent.pack_id == pack.id).cte()
    current_cycle = PilotUsageEvent.id > func.coalesce(refills.c.last_refill_usage, 0)
    count, volume_used_ml, last_use, usage_count, refill_count, refill_at = db.execute(
        select(
            func.count(PilotUsageEvent.id).filter(current_cycle),
            func.coalesce(func.sum(PilotUsageEvent.volume_used_ml).filter(current_cycle), 0),
            func.max(PilotUsageEvent.occurred_at).filter(current_cycle),
            func.count(PilotUsageEvent.id),
            refills.c.refill_count,
            refills.c.last_refill_at,
        ).select_from(refills).outerjoin(
            PilotUsageEvent, PilotUsageEvent.pack_id == pack.id
        ).group_by(refills.c.refill_count, refills.c.last_refill_at)
    ).one()
    remaining = CAPACITY_ML - volume_used_ml
    points = usage_count * USAGE_POINTS + refill_count * REFILL_POINTS
    cycle_start = refill_at or pack.started_at
    started_at = _to_utc(cycle_start) if cycle_start is not None else None
    elapsed_days = (now - started_at).total_seconds() / 86400 if started_at else 0.0
    result = PackStatus(
        started_at=started_at,
        last_refill_at=_to_utc(refill_at) if refill_at is not None else None,
        current_volume_l=remaining / 1000,
        capacity_l=CAPACITY_ML / 1000,
        dose_ml=DOSE_ML,
        full_doses_remaining=remaining // DOSE_ML,
        sample_count=count,
        user_points=points,
        cashback_earned_cents=(points // POINTS_PER_EURO) * 100,
        points_to_next_cashback=POINTS_PER_EURO - points % POINTS_PER_EURO,
        data_span_days=elapsed_days,
    )
    if remaining == 0:
        result.days_remaining = 0.0
        result.estimated_runout_at = _to_utc(last_use)
    if count >= 2 and elapsed_days >= 2.0:
        rate = volume_used_ml / elapsed_days
        result.consumption_rate_l_day = rate / 1000
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
    return record_pack_event(db, user, tap.request_id, language, refill=False)


@router.post("/pack/refill", response_model=UsageResult)
def record_refill(
    event: UsageTap,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
):
    return record_pack_event(db, user, event.request_id, language, refill=True)


def record_pack_event(db, user, request_id, language, *, refill):
    pack = get_pack(db, user, lock=True)
    if pack.started_at is None:
        raise HTTPException(409, "Start the pilot pack before logging events")

    model = PilotRefillEvent if refill else PilotUsageEvent
    request_id = str(request_id)
    duplicate = db.scalar(select(model.id).where(
        model.pack_id == pack.id, model.request_id == request_id
    ))
    now = datetime.now(timezone.utc)
    before = pack_status(db, pack, now)
    result = before
    points_earned = 0
    if duplicate is None:
        if refill:
            if before.current_volume_l == before.capacity_l:
                raise HTTPException(409, "The pilot pack is already full")
            latest_usage_id = db.scalar(select(func.max(PilotUsageEvent.id)).where(
                PilotUsageEvent.pack_id == pack.id
            ))
            db.add(PilotRefillEvent(
                pack_id=pack.id, request_id=request_id, after_usage_id=latest_usage_id,
                occurred_at=now,
            ))
            points_earned = REFILL_POINTS
        else:
            if before.current_volume_l == 0:
                raise HTTPException(409, "The pilot pack is empty")
            db.add(PilotUsageEvent(
                pack_id=pack.id, request_id=request_id,
                volume_used_ml=min(DOSE_ML, round(before.current_volume_l * 1000)),
                occurred_at=now,
            ))
            points_earned = USAGE_POINTS
        db.flush()
        result = pack_status(db, pack, now)

    language = language or message_language(db, user.id)
    db.commit()
    unlocked = result.cashback_earned_cents - before.cashback_earned_cents
    oracle = stock_message(
        result, language, points_earned=points_earned, cashback_unlocked_cents=unlocked,
        event="refill" if refill else "usage", generate=bool(points_earned),
    )
    return UsageResult(
        **result.model_dump(exclude_computed_fields=True), points_earned=points_earned,
        refill_recorded=refill and duplicate is None,
        cashback_unlocked_cents=unlocked, oracle=oracle,
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
