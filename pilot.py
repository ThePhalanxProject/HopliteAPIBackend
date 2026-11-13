from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, computed_field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from consumption import _to_utc
from pilot_oracle import OracleMessage, PriceSearch, oracle_copy, pilot_prices, stock_message, stock_message_payload
from pilot_price_progress import price_search_snapshot, run_price_search
from database import Base, SessionLocal, engine
from models import PilotBurstRefillAlert, PilotMessagePreference, PilotPack, PilotPriceSearch, PilotRefillAlert, PilotRefillEvent, PilotRefillOracle, PilotSession, PilotUsageEvent, PilotUsageOracle, PilotUser

CAPACITY_ML = 1500
DOSE_ML = 65
FORECAST_WINDOWS = (
    timedelta(hours=1), timedelta(days=1), timedelta(days=7),
    timedelta(days=15), timedelta(days=30),
)
HOUR_WINDOW, DAY_WINDOW = FORECAST_WINDOWS[:2]
COLD_START_BURST_USES = 8
REFILL_FORECAST_DAYS = 7
USAGE_POINTS = 5
REFILL_POINTS = 10
POINTS_PER_EURO = 50
REFILL_ALERT_MESSAGES = {
    "en": "Your detergent is running low. Please refill your Hoplite.",
    "fr": "Votre lessive est presque épuisée. Pensez à recharger votre Hoplite.",
}

BURST_REFILL_ALERT_MESSAGES = {
    "en": "High usage brought forward your refill. Please refill your Hoplite.",
    "fr": "Votre forte utilisation a avancé le besoin de recharge. Pensez à recharger votre Hoplite.",
}

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
    usage_overuse: dict = Field(default_factory=dict, exclude=True)
    refill_needed: bool = False
    refill_reason: Literal["high_utilization", "normal"] | None = None
    refill_alert_message: str | None = None

    @computed_field
    @property
    def stock_status(self) -> Literal["not_started", "ok", "low", "urgent", "empty"]:
        if self.started_at is None:
            return "not_started"
        if self.current_volume_l == 0:
            return "empty"
        if self.full_doses_remaining < 3 or (
            self.days_remaining is not None and self.days_remaining <= 3
        ):
            return "urgent"
        if self.refill_needed or (self.days_remaining is not None and self.days_remaining <= REFILL_FORECAST_DAYS):
            return "low"
        return "ok"


class UsageResult(PackStatus):
    points_earned: int
    refill_recorded: bool
    cashback_unlocked_cents: int
    oracle: OracleMessage
    oracle_url: str | None = None


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


def consumption_outlook(db: Session, pack: PilotPack, now: datetime, cycle_start: datetime):
    tracked_since = _to_utc(pack.started_at)
    timestamp = PilotUsageEvent.occurred_at
    cutoffs = [now - window for window in FORECAST_WINDOWS]
    baseline_end = cutoffs[1]
    baseline_start = max(tracked_since, baseline_end - FORECAST_WINDOWS[-1])
    *volumes, hourly_uses, daily_uses, baseline_uses = db.execute(
        select(
            *(func.coalesce(func.sum(PilotUsageEvent.volume_used_ml).filter(
                timestamp > cutoff,
                timestamp >= (cycle_start if index == 0 else tracked_since)
            ), 0) for index, cutoff in enumerate(cutoffs)),
            func.count().filter(timestamp > cutoffs[0], timestamp >= cycle_start),
            func.count().filter(timestamp > cutoffs[1]),
            func.count().filter(timestamp <= baseline_end),
        ).where(
            PilotUsageEvent.pack_id == pack.id, timestamp >= baseline_start, timestamp <= now,
        )
    ).one()

    baseline_days = (baseline_end - baseline_start) / DAY_WINDOW
    has_baseline = baseline_days >= 7 and baseline_uses >= 3
    baseline_daily = baseline_uses / baseline_days if has_baseline else None
    hourly_overuse = (
        hourly_uses >= 3 and hourly_uses >= 2 * baseline_daily / 24
        if has_baseline else hourly_uses >= COLD_START_BURST_USES
    )
    daily_overuse = has_baseline and daily_uses >= 4 and daily_uses >= 2 * baseline_daily
    rates = [
        volume / (window / DAY_WINDOW)
        for window, volume in zip(FORECAST_WINDOWS[1:], volumes[1:])
        if now - tracked_since >= window
    ]
    regular_rate = sum(rates) / len(rates) if rates else None
    # A short burst should influence the forecast when there is evidence of overuse.
    if hourly_overuse:
        rates.append(volumes[0] / (HOUR_WINDOW / DAY_WINDOW))
    return (
        sum(rates) / len(rates) if rates else None,
        regular_rate,
        {
            "rapid_usage": hourly_overuse,
            "uses_in_last_hour": hourly_uses,
            "daily_overuse": daily_overuse,
            "uses_in_last_day": daily_uses,
            "historical_uses_per_day": round(baseline_daily, 2) if has_baseline else None,
        },
    )


def pack_status(db: Session, pack: PilotPack, now: datetime, language: str) -> PackStatus:
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
    rate = regular_rate = None
    if started_at is not None and count:
        rate, regular_rate, result.usage_overuse = consumption_outlook(db, pack, now, started_at)
    if rate:
        result.consumption_rate_l_day = rate / 1000
        if remaining > 0:
            result.days_remaining = remaining / rate
            result.estimated_runout_at = now + timedelta(days=result.days_remaining)
    alert = db.get(PilotRefillAlert, pack.id)
    active = alert is not None and _to_utc(alert.cycle_started_at) == started_at
    burst_alert = db.get(PilotBurstRefillAlert, pack.id) if active else None
    if not active and result.stock_status in {"low", "urgent", "empty"}:
        if alert is None:
            db.add(PilotRefillAlert(pack_id=pack.id, cycle_started_at=started_at))
        else:
            alert.cycle_started_at = started_at
        if (
            result.usage_overuse.get("rapid_usage")
            and result.full_doses_remaining >= 3
            and (not regular_rate or remaining / regular_rate > REFILL_FORECAST_DAYS)
        ):
            burst_alert = db.get(PilotBurstRefillAlert, pack.id)
            if burst_alert is None:
                burst_alert = PilotBurstRefillAlert(pack_id=pack.id, cycle_started_at=started_at)
                db.add(burst_alert)
            else:
                burst_alert.cycle_started_at = started_at
        active = True
    if active:
        result.refill_needed = True
        burst_triggered = burst_alert is not None and _to_utc(burst_alert.cycle_started_at) == started_at
        result.refill_reason = "high_utilization" if burst_triggered else "normal"
        result.refill_alert_message = (
            BURST_REFILL_ALERT_MESSAGES[language] if burst_triggered else REFILL_ALERT_MESSAGES[language]
        )
    return result


@router.get("/pack", response_model=PackStatus)
def read_pack(user: PilotUser = Depends(participant), db: Session = Depends(get_db)):
    pack = get_pack(db, user, lock=True)
    result = pack_status(db, pack, datetime.now(timezone.utc), message_language(db, user.id))
    db.commit()
    return result


@router.post("/pack/start", response_model=PackStatus)
def start_pack(user: PilotUser = Depends(participant), db: Session = Depends(get_db)):
    pack = get_pack(db, user, lock=True)
    now = datetime.now(timezone.utc)
    if pack.started_at is None:
        pack.started_at = now
    result = pack_status(db, pack, now, message_language(db, user.id))
    db.commit()
    return result


@router.post("/pack/usage", response_model=UsageResult)
def record_usage(
    tap: UsageTap,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
    defer_oracle: bool = Query(False),
):
    return record_pack_event(
        db, user, tap.request_id, language, refill=False, defer_oracle=defer_oracle
    )


@router.post("/pack/refill", response_model=UsageResult)
def record_refill(
    event: UsageTap,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
    defer_oracle: bool = Query(False),
):
    return record_pack_event(
        db, user, event.request_id, language, refill=True, defer_oracle=defer_oracle,
    )


def usage_message_context(db: Session, pack_id: int, now: datetime) -> dict:
    last_use = db.scalar(select(func.max(PilotUsageEvent.occurred_at)).where(
        PilotUsageEvent.pack_id == pack_id,
    ))
    elapsed = now - _to_utc(last_use) if last_use is not None else None
    return {
        "days_since_previous_use": elapsed.days if elapsed is not None and elapsed >= timedelta(days=7) else None,
    }


def record_pack_event(db, user, request_id, language, *, refill, defer_oracle=False):
    pack = get_pack(db, user, lock=True)
    if pack.started_at is None:
        raise HTTPException(409, "Start the pilot pack before logging events")

    model = PilotRefillEvent if refill else PilotUsageEvent
    request_id = str(request_id)
    duplicate = db.scalar(select(model.id).where(
        model.pack_id == pack.id, model.request_id == request_id
    ))
    now = datetime.now(timezone.utc)
    language = language or message_language(db, user.id)
    before = pack_status(db, pack, now, language)
    result = before
    points_earned = 0
    usage_context = None
    pack_event = None
    if duplicate is None:
        if refill:
            if before.current_volume_l == before.capacity_l:
                raise HTTPException(409, "The pilot pack is already full")
            latest_usage_id = db.scalar(select(func.max(PilotUsageEvent.id)).where(
                PilotUsageEvent.pack_id == pack.id
            ))
            pack_event = PilotRefillEvent(
                pack_id=pack.id, request_id=request_id, after_usage_id=latest_usage_id,
                occurred_at=now,
            )
            points_earned = REFILL_POINTS
        else:
            if before.current_volume_l == 0:
                raise HTTPException(409, "The pilot pack is empty")
            usage_context = usage_message_context(db, pack.id, now)
            pack_event = PilotUsageEvent(
                pack_id=pack.id, request_id=request_id,
                volume_used_ml=min(DOSE_ML, round(before.current_volume_l * 1000)),
                occurred_at=now,
            )
            points_earned = USAGE_POINTS
        db.add(pack_event)
        db.flush()
        result = pack_status(db, pack, now, language)
        if usage_context is not None:
            usage_context.update(result.usage_overuse)
            previous_pct = before.current_volume_l / before.capacity_l * 100
            remaining_pct = result.current_volume_l / result.capacity_l * 100
            usage_context["stock_milestone_percent"] = next(
                (level for level in (10, 25, 50) if remaining_pct <= level < previous_pct),
                None,
            )

    unlocked = result.cashback_earned_cents - before.cashback_earned_cents
    oracle_url = None
    if defer_oracle:
        oracle_model = PilotRefillOracle if refill else PilotUsageOracle
        event_kind = "refill" if refill else "usage"
        if pack_event is not None:
            event_key = {"refill_id" if refill else "usage_id": pack_event.id}
            db.add(oracle_model(
                **event_key,
                payload=stock_message_payload(
                    result, language, points_earned=points_earned,
                    cashback_unlocked_cents=unlocked, event=event_kind, usage_context=usage_context,
                ),
            ))
            oracle_url = f"/pilot/pack/{event_kind}/{request_id}/oracle"
        elif duplicate is not None and db.get(oracle_model, duplicate) is not None:
            oracle_url = f"/pilot/pack/{event_kind}/{request_id}/oracle"

    db.commit()
    if defer_oracle:
        oracle = OracleMessage(
            stock_status=result.stock_status, message=None,
            source="deferred" if oracle_url else "skipped",
            refill_alert_message=result.refill_alert_message,
        )
    else:
        oracle = stock_message(
            result, language, points_earned=points_earned, cashback_unlocked_cents=unlocked,
            event="refill" if refill else "usage", usage_context=usage_context,
            generate=bool(points_earned),
        )
    return UsageResult(
        **result.model_dump(exclude_computed_fields=True), points_earned=points_earned,
        refill_recorded=refill and duplicate is None,
        cashback_unlocked_cents=unlocked, oracle=oracle, oracle_url=oracle_url,
    )


@router.post("/pack/usage/{request_id}/oracle", response_model=OracleMessage)
def read_usage_oracle(
    request_id: UUID,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
):
    return read_event_oracle(request_id, user, db, refill=False)


@router.post("/pack/refill/{request_id}/oracle", response_model=OracleMessage)
def read_refill_oracle(
    request_id: UUID,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
):
    return read_event_oracle(request_id, user, db, refill=True)


def read_event_oracle(request_id: UUID, user: PilotUser, db: Session, *, refill: bool):
    pack = get_pack(db, user)
    event_model = PilotRefillEvent if refill else PilotUsageEvent
    oracle_model = PilotRefillOracle if refill else PilotUsageOracle
    event_id = oracle_model.refill_id if refill else oracle_model.usage_id
    saved = db.execute(
        select(event_id, oracle_model.payload, oracle_model.message).join(event_model).where(
            event_model.pack_id == pack.id,
            event_model.request_id == str(request_id),
        )
    ).one_or_none()
    if saved is None:
        raise HTTPException(404, "Refill message not found" if refill else "Usage message not found")

    saved_id, payload, message = saved
    db.rollback()
    if message is None:
        message, _ = oracle_copy(payload, None)
        if message is not None:
            db.execute(update(oracle_model).where(
                event_id == saved_id, oracle_model.message.is_(None)
            ).values(message=message))
            db.commit()
            message = db.scalar(select(oracle_model.message).where(
                event_id == saved_id
            ))
    return OracleMessage(
        stock_status=payload["stock_status"],
        message=message,
        source="kimi" if message else "unavailable",
    )


@router.get("/pack/oracle", response_model=OracleMessage)
def read_oracle(
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    language: str | None = Query(None, pattern="^(en|fr)$"),
):
    pack = get_pack(db, user, lock=True)
    language = language or message_language(db, user.id)
    status = pack_status(db, pack, datetime.now(timezone.utc), language)
    db.commit()
    return stock_message(status, language)


@router.post("/prices")
def search_prices(
    query: PriceSearch,
    tasks: BackgroundTasks,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
    progressive: bool = False,
):
    user_id = user.id
    language = query.language or message_language(db, user_id)
    if progressive:
        now = datetime.now(timezone.utc)
        db.execute(delete(PilotPriceSearch).where(
            PilotPriceSearch.created_at < now - timedelta(hours=1)
        ))
        search = PilotPriceSearch(
            id=str(uuid4()), user_id=user_id, created_at=now, status="searching",
        )
        snapshot = price_search_snapshot(search, now)
        db.add(search)
        db.commit()
        tasks.add_task(run_price_search, snapshot["search_id"], query, language)
        return snapshot

    db.rollback()
    return pilot_prices(
        query.brand, language, query.city, query.country,
        refresh=query.refresh, postal_code=query.postal_code,
    )


@router.get("/prices/{search_id}")
def read_price_search(
    search_id: UUID,
    user: PilotUser = Depends(participant),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    search = db.scalar(select(PilotPriceSearch).where(
        PilotPriceSearch.id == str(search_id),
        PilotPriceSearch.user_id == user.id,
        PilotPriceSearch.created_at >= now - timedelta(hours=1),
    ))
    if search is None:
        raise HTTPException(404, "Price search not found")
    return price_search_snapshot(search, now)


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
