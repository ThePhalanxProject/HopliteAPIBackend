from sqlalchemy import Column, Integer, Float, String, DateTime, Text, JSON, ForeignKey, UniqueConstraint
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
