"""Persistent cashback and reset history. Callers hold the user's pack lock."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, literal, select, union_all

from models import PilotAccountResetEvent, PilotCashbackMilestone, PilotRefillEvent, PilotUsageEvent

USAGE_POINTS = 5
REFILL_POINTS = 10
POINTS_PER_EURO = 100


class MilestoneView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_number: int
    milestone_number: int
    points_threshold: int
    cashback_cents: int
    reached_at: datetime
    recorded_at: datetime
    source_request_id: UUID
    source_event_type: Literal["usage", "refill"]
    reconstructed: bool


class ResetEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reset_number: int
    request_id: UUID | None
    occurred_at: datetime
    points_before_reset: int


class AccountHistoryView(BaseModel):
    current_user_points: int
    account_reset_count: int
    lifetime_cashback_milestone_count: int
    lifetime_cashback_earned_cents: int
    milestones: list[MilestoneView]
    resets: list[ResetEventView]
    limit: int
    offset: int


def sync_cashback_history(db, pack, points, now, *, current_request_id=None):
    """Record new thresholds, recovering any still-visible pre-deployment events.

    Only scan the event timeline when an unrecorded threshold exists. A refill
    continues the current points cycle; an account reset starts another cycle.
    Events and their history are committed together by the caller.
    """
    reset_count = db.scalar(select(func.coalesce(func.max(PilotAccountResetEvent.reset_number), 0)).where(
        PilotAccountResetEvent.user_id == pack.user_id,
    ))
    count, earned_cents, last_milestone = db.execute(select(
        func.count(PilotCashbackMilestone.id),
        func.coalesce(func.sum(PilotCashbackMilestone.cashback_cents), 0),
        func.coalesce(func.max(PilotCashbackMilestone.milestone_number).filter(
            PilotCashbackMilestone.cycle_number == reset_count,
        ), 0),
    ).where(PilotCashbackMilestone.user_id == pack.user_id)).one()
    if points // POINTS_PER_EURO > last_milestone:
        timeline = union_all(
            select(
                PilotUsageEvent.occurred_at.label("occurred_at"),
                PilotUsageEvent.id.label("usage_order"), literal(0).label("event_order"),
                PilotUsageEvent.id.label("event_id"), PilotUsageEvent.request_id.label("request_id"),
                literal("usage").label("event_type"), literal(USAGE_POINTS).label("points"),
            ).where(PilotUsageEvent.pack_id == pack.id),
            select(
                PilotRefillEvent.occurred_at, PilotRefillEvent.after_usage_id, literal(1),
                PilotRefillEvent.id, PilotRefillEvent.request_id, literal("refill"), literal(REFILL_POINTS),
            ).where(PilotRefillEvent.pack_id == pack.id),
        ).subquery()
        running_points = 0
        for event in db.execute(select(timeline).order_by(
            timeline.c.occurred_at, timeline.c.usage_order, timeline.c.event_order, timeline.c.event_id,
        )):
            running_points += event.points
            number = running_points // POINTS_PER_EURO
            if number <= last_milestone:
                continue
            db.add(PilotCashbackMilestone(
                user_id=pack.user_id, cycle_number=reset_count, milestone_number=number,
                points_threshold=number * POINTS_PER_EURO, cashback_cents=100,
                reached_at=event.occurred_at, recorded_at=now,
                source_request_id=event.request_id, source_event_type=event.event_type,
                reconstructed=event.request_id != current_request_id,
            ))
            last_milestone = number
            count += 1
            earned_cents += 100
        db.flush()
    return {
        "account_reset_count": reset_count,
        "lifetime_cashback_milestone_count": count,
        "lifetime_cashback_earned_cents": earned_cents,
    }
