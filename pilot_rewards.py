"""Database-only earned rewards, with idempotent source credits."""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import PilotCashbackMilestone, PilotPack, PilotRefillEvent, PilotRefillSurvey, PilotRewardAccount, PilotRewardCredit, PilotUsageEvent, PilotUser

SURVEY_REWARD_CENTS = 500


def ensure_reward_account(db, user_id):
    account = db.get(PilotRewardAccount, user_id)
    if account is not None:
        return account
    # All creation/credit paths serialize on the user row. Normal activity
    # callers acquire the pack lock first; this helper never acquires a pack lock.
    db.scalar(select(PilotUser.id).where(PilotUser.id == user_id).with_for_update())
    account = db.scalar(select(PilotRewardAccount).where(
        PilotRewardAccount.user_id == user_id,
    ).execution_options(populate_existing=True))
    if account is None:
        now = datetime.now(timezone.utc)
        account = PilotRewardAccount(user_id=user_id, balance_cents=0, created_at=now, updated_at=now)
        db.add(account)
        db.flush()
    return account


def sync_reward_account(db, user_id):
    """Credit only uncredited source rows; caller commits the entire transaction."""
    db.scalar(select(PilotUser.id).where(PilotUser.id == user_id).with_for_update())
    ensure_reward_account(db, user_id)
    account = db.scalar(select(PilotRewardAccount).where(
        PilotRewardAccount.user_id == user_id,
    ).with_for_update().execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    added = 0
    milestones = db.scalars(select(PilotCashbackMilestone).outerjoin(
        PilotRewardCredit, PilotRewardCredit.milestone_id == PilotCashbackMilestone.id,
    ).where(PilotCashbackMilestone.user_id == user_id, PilotRewardCredit.id.is_(None)))
    for milestone in milestones:
        db.add(PilotRewardCredit(
            user_id=user_id, reward_type="points", milestone_id=milestone.id,
            amount_cents=milestone.cashback_cents, earned_at=milestone.reached_at, credited_at=now,
        ))
        added += milestone.cashback_cents
    surveys = db.scalars(select(PilotRefillSurvey).outerjoin(
        PilotRewardCredit, PilotRewardCredit.survey_id == PilotRefillSurvey.id,
    ).where(PilotRefillSurvey.user_id == user_id, PilotRefillSurvey.submitted_at.is_not(None),
            PilotRewardCredit.id.is_(None)))
    for survey in surveys:
        db.add(PilotRewardCredit(
            user_id=user_id, reward_type="survey", survey_id=survey.id,
            amount_cents=SURVEY_REWARD_CENTS, earned_at=survey.submitted_at, credited_at=now,
        ))
        added += SURVEY_REWARD_CENTS
    if added:
        account.balance_cents += added
        account.updated_at = now
    db.flush()
    return account


def initialize_reward_accounts(engine):
    """Populate all accounts from retained activity/history without external calls."""
    # Imported at call time to avoid a circular import: new point milestones also
    # call sync_reward_account in the same transaction that records their source.
    from pilot_account_history import REFILL_POINTS, USAGE_POINTS, sync_cashback_history
    with engine.connect() as connection:
        user_ids = list(connection.scalars(select(PilotUser.id).order_by(PilotUser.id)))
    for user_id in user_ids:
        with Session(engine, autoflush=False) as db:
            pack = db.scalar(select(PilotPack).where(PilotPack.user_id == user_id).with_for_update())
            if pack is not None:
                uses = db.scalar(select(func.count(PilotUsageEvent.id)).where(PilotUsageEvent.pack_id == pack.id))
                refills = db.scalar(select(func.count(PilotRefillEvent.id)).where(PilotRefillEvent.pack_id == pack.id))
                sync_cashback_history(db, pack, uses * USAGE_POINTS + refills * REFILL_POINTS,
                                      datetime.now(timezone.utc))
            sync_reward_account(db, user_id)
            db.commit()
