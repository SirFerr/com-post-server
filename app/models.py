import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    USER = "USER"
    MODERATOR = "MODERATOR"
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"


class SessionStatus(str, enum.Enum):
    OPEN_REQUESTED = "OPEN_REQUESTED"
    OPENED = "OPENED"
    PHOTO_UPLOADED = "PHOTO_UPLOADED"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Composter(Base):
    __tablename__ = "composters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    device_id: Mapped[str] = mapped_column(String(80), unique=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[float] = mapped_column(Float, default=100)
    secret: Mapped[str] = mapped_column(String(128))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    fill_level: Mapped[int] = mapped_column(Integer, default=0)
    battery_level: Mapped[int] = mapped_column(Integer, default=100)
    lock_state: Mapped[str] = mapped_column(String(20), default="CLOSED")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccessSession(Base):
    __tablename__ = "access_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    composter_id: Mapped[str] = mapped_column(ForeignKey("composters.id"), index=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.OPEN_REQUESTED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship()
    composter: Mapped[Composter] = relationship()


class DeviceCommand(Base):
    __tablename__ = "device_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("access_sessions.id"), index=True)
    action: Mapped[str] = mapped_column(String(10))
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("access_sessions.id"), unique=True)
    photo_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING)
    ml_status: Mapped[str] = mapped_column(String(40), default="NEEDS_MANUAL_REVIEW")
    ml_confidence: Mapped[float] = mapped_column(Float, default=0)
    ml_violations: Mapped[str] = mapped_column(Text, default="[]")
    violation_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Violation(Base):
    __tablename__ = "violations"
    __table_args__ = (UniqueConstraint("review_id", name="uq_violation_review"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"))
    reason: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserScore(Base):
    __tablename__ = "user_scores"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    total_uploads: Mapped[int] = mapped_column(Integer, default=0)
    valid_uploads: Mapped[int] = mapped_column(Integer, default=0)


class Telemetry(Base):
    __tablename__ = "composter_telemetry"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    composter_id: Mapped[str] = mapped_column(ForeignKey("composters.id"), index=True)
    battery_level: Mapped[int] = mapped_column(Integer)
    fill_level: Mapped[int] = mapped_column(Integer)
    lock_state: Mapped[str] = mapped_column(String(20))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
