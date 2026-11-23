from sqlalchemy import Column, Boolean, CheckConstraint, Integer, Float, String, DateTime, Text, JSON, ForeignKey, UniqueConstraint
from datetime import datetime

from database import Base


class MeasurementModel(Base):
    __tablename__ = "measurements"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String)
    weight = Column(Float)
    battery = Column(Float)
    timestamp = Column(DateTime, default=datetime.utcnow)


class NotificationModel(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True)
    notification_type = Column(String)
    priority = Column(String)
    message = Column(Text)
    amazon_option = Column(String, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="generated")


class PilotUser(Base):
    __tablename__ = "pilot_users"

    id = Column(Integer, primary_key=True)
    participant_code = Column(String, nullable=False, unique=True)
    token_hash = Column(String(64), nullable=False, unique=True)


class PilotPack(Base):
    __tablename__ = "pilot_packs"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, unique=True)
    started_at = Column(DateTime(timezone=True))


class PilotUsageEvent(Base):
    __tablename__ = "pilot_usage_events"
    __table_args__ = (UniqueConstraint("pack_id", "request_id"),)

    id = Column(Integer, primary_key=True)
    pack_id = Column(ForeignKey("pilot_packs.id"), nullable=False)
    request_id = Column(String(36), nullable=False)
    # Keep the existing SQL column so prior simulated pilot taps remain readable.
    volume_used_ml = Column("grams_used", Integer, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)


class PilotMessagePreference(Base):
    __tablename__ = "pilot_message_preferences"

    user_id = Column(ForeignKey("pilot_users.id"), primary_key=True)
    language = Column(String(2), nullable=False)


class PilotLogin(Base):
    __tablename__ = "pilot_logins"

    user_id = Column(ForeignKey("pilot_users.id"), primary_key=True)
    email = Column(String(254), unique=True, nullable=False)
    display_name = Column(String(100), nullable=False)
    password_hash = Column(String(128), nullable=False)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True))


class PilotSession(Base):
    __tablename__ = "pilot_sessions"

    token_hash = Column(String(64), primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class PilotRefillEvent(Base):
    __tablename__ = "pilot_refill_events"
    __table_args__ = (UniqueConstraint("pack_id", "request_id"),)

    id = Column(Integer, primary_key=True)
    pack_id = Column(ForeignKey("pilot_packs.id"), nullable=False)
    request_id = Column(String(36), nullable=False)
    after_usage_id = Column(ForeignKey("pilot_usage_events.id"), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)


class PilotRefillAlert(Base):
    __tablename__ = "pilot_refill_alerts"

    pack_id = Column(ForeignKey("pilot_packs.id"), primary_key=True)
    cycle_started_at = Column(DateTime(timezone=True), nullable=False)


class PilotUsageOracle(Base):
    __tablename__ = "pilot_usage_oracle"

    usage_id = Column(ForeignKey("pilot_usage_events.id"), primary_key=True)
    payload = Column(JSON, nullable=False)
    message = Column(Text)


class PilotPriceSearch(Base):
    __tablename__ = "pilot_price_searches"

    id = Column(String(36), primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), index=True, nullable=False)
    status = Column(String(20), nullable=False)
    result = Column(JSON, nullable=True)


class PilotRefillOracle(Base):
    __tablename__ = "pilot_refill_oracle"

    refill_id = Column(ForeignKey("pilot_refill_events.id"), primary_key=True)
    payload = Column(JSON, nullable=False)
    message = Column(Text)


class PilotBurstRefillAlert(Base):
    __tablename__ = "pilot_burst_refill_alerts"

    pack_id = Column(ForeignKey("pilot_packs.id"), primary_key=True)
    cycle_started_at = Column(DateTime(timezone=True), nullable=False)


class PilotOfferPreference(Base):
    __tablename__ = "pilot_offer_preferences"
    __table_args__ = (UniqueConstraint("user_id", "request_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, index=True)
    request_id = Column(String(36), nullable=False)
    search_id = Column(String(36), nullable=False)
    url = Column(Text, nullable=False)
    retailer = Column(String, nullable=False)
    brand = Column(String, nullable=False)
    country = Column(String(2), nullable=False)
    action = Column(String(10), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)


class PilotRefillSurvey(Base):
    __tablename__ = "pilot_refill_surveys"
    __table_args__ = (UniqueConstraint("user_id", "refill_request_id"),)

    id = Column(String(36), primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, index=True)
    # Keep the request UUID independently of resettable refill activity.
    refill_request_id = Column(String(36), nullable=False)
    refilled_at = Column(DateTime(timezone=True), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    language = Column(String(2), nullable=False)
    submitted_at = Column(DateTime(timezone=True))
    enjoyed_application = Column(Boolean)
    would_use_connected_product = Column(Boolean)
    found_useful = Column(Boolean)
    experience_feedback = Column(Text)


class PilotUsageSurveyTrigger(Base):
    __tablename__ = "pilot_usage_survey_triggers"

    survey_id = Column(ForeignKey("pilot_refill_surveys.id"), primary_key=True)
    request_id = Column(String(36), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)


class PilotSurveyAdditionalAnswers(Base):
    """Version-two answers without altering the deployed survey table."""
    __tablename__ = "pilot_survey_additional_answers"

    survey_id = Column(ForeignKey("pilot_refill_surveys.id"), primary_key=True)
    share_anonymous_usage_data = Column(Boolean, nullable=False)
    would_pay_for_device = Column(Boolean, nullable=False)


class PilotSurveyCycle(Base):
    __tablename__ = "pilot_survey_cycles"

    user_id = Column(ForeignKey("pilot_users.id"), primary_key=True)
    started_at = Column(DateTime(timezone=True), nullable=False)


class PilotSurveyAnswers(Base):
    """All six answers to the revised survey, linked to its user through survey_id."""
    __tablename__ = "pilot_survey_answers"

    survey_id = Column(ForeignKey("pilot_refill_surveys.id"), primary_key=True)
    enjoyed_application = Column(Boolean, nullable=False)
    price_comparison_beneficial = Column(Boolean, nullable=False)
    would_buy_automatic_device = Column(Boolean, nullable=False)
    would_pay_for_device = Column(Boolean, nullable=False)
    share_anonymous_usage_and_demographic_data = Column(Boolean, nullable=False)
    comments = Column(Text, nullable=False)


class PilotCashbackMilestone(Base):
    """Permanent record of each 100-point threshold, surviving activity resets."""
    __tablename__ = "pilot_cashback_milestones"
    __table_args__ = (UniqueConstraint("user_id", "cycle_number", "milestone_number"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, index=True)
    cycle_number = Column(Integer, nullable=False)
    milestone_number = Column(Integer, nullable=False)
    points_threshold = Column(Integer, nullable=False)
    cashback_cents = Column(Integer, nullable=False)
    reached_at = Column(DateTime(timezone=True), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False)
    source_request_id = Column(String(36), nullable=False)
    source_event_type = Column(String(6), nullable=False)
    reconstructed = Column(Boolean, nullable=False)


class PilotAccountResetEvent(Base):
    """Successful account resets; no foreign key to activity that will be deleted."""
    __tablename__ = "pilot_account_resets"
    __table_args__ = (
        UniqueConstraint("user_id", "reset_number"),
        UniqueConstraint("user_id", "request_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("pilot_users.id"), nullable=False, index=True)
    reset_number = Column(Integer, nullable=False)
    request_id = Column(String(36))
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    points_before_reset = Column(Integer, nullable=False)


class PilotRewardAccount(Base):
    """Persistent earned euro balance, held in integer cents and never reset by activity reset."""
    __tablename__ = "pilot_reward_accounts"
    __table_args__ = (CheckConstraint("balance_cents >= 0"),)

    user_id = Column(ForeignKey("pilot_users.id"), primary_key=True)
    balance_cents = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class PilotRewardCredit(Base):
    """One auditable credit per point milestone or submitted survey."""
    __tablename__ = "pilot_reward_credits"
    __table_args__ = (
        CheckConstraint("amount_cents > 0"),
        CheckConstraint(
            "(reward_type = 'points' AND milestone_id IS NOT NULL AND survey_id IS NULL) OR "
            "(reward_type = 'survey' AND survey_id IS NOT NULL AND milestone_id IS NULL)"
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("pilot_reward_accounts.user_id"), nullable=False, index=True)
    reward_type = Column(String(6), nullable=False)
    milestone_id = Column(ForeignKey("pilot_cashback_milestones.id"), unique=True)
    survey_id = Column(ForeignKey("pilot_refill_surveys.id"), unique=True)
    amount_cents = Column(Integer, nullable=False)
    earned_at = Column(DateTime(timezone=True), nullable=False)
    credited_at = Column(DateTime(timezone=True), nullable=False)
