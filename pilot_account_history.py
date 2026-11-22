"""Persistent cashback and reset history. Callers hold the user's pack lock."""
from sqlalchemy import func, literal, select, union_all

from models import PilotAccountResetEvent, PilotCashbackMilestone, PilotRefillEvent, PilotUsageEvent

USAGE_POINTS = 5
REFILL_POINTS = 10
POINTS_PER_REWARD = 100
CASHBACK_REWARD_CENTS = 500
POINTS_PER_EURO = POINTS_PER_REWARD * 100 // CASHBACK_REWARD_CENTS


def account_reset_count(db, user_id):
    return db.scalar(select(func.coalesce(func.max(PilotAccountResetEvent.reset_number), 0)).where(
        PilotAccountResetEvent.user_id == user_id,
    ))


def sync_cashback_history(db, pack, points, now, *, current_request_id=None):
    """Record new thresholds, recovering any still-visible pre-deployment events.

    Only scan the event timeline when an unrecorded threshold exists. A refill
    continues the current points cycle; an account reset starts another cycle.
    Events and their history are committed together by the caller.
    """
    reset_count = account_reset_count(db, pack.user_id)
    last_milestone, earned_cents = db.execute(select(
        func.coalesce(func.max(PilotCashbackMilestone.milestone_number), 0),
        func.coalesce(func.sum(PilotCashbackMilestone.cashback_cents), 0),
    ).where(
        PilotCashbackMilestone.user_id == pack.user_id,
        PilotCashbackMilestone.cycle_number == reset_count,
    )).one()
    if points // POINTS_PER_REWARD > last_milestone:
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
            number = running_points // POINTS_PER_REWARD
            if number <= last_milestone:
                continue
            db.add(PilotCashbackMilestone(
                user_id=pack.user_id, cycle_number=reset_count, milestone_number=number,
                points_threshold=number * POINTS_PER_REWARD, cashback_cents=CASHBACK_REWARD_CENTS,
                reached_at=event.occurred_at, recorded_at=now,
                source_request_id=event.request_id, source_event_type=event.event_type,
                reconstructed=event.request_id != current_request_id,
            ))
            last_milestone = number
            earned_cents += CASHBACK_REWARD_CENTS
        db.flush()
    return earned_cents
