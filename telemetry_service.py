"""Telemetry history service with a private database and idempotent event API."""

import os
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.observability import ObservabilityMiddleware, metrics_text


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    composter_id: Mapped[str] = mapped_column(String(36), index=True)
    battery_level: Mapped[int] = mapped_column(Integer)
    fill_level: Mapped[int] = mapped_column(Integer)
    lock_state: Mapped[str] = mapped_column(String(20))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)


database_url = os.getenv("TELEMETRY_DATABASE_URL", "sqlite:///./telemetry.db")
engine = create_engine(database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Event(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    kind: str
    composter_id: str = Field(min_length=1, max_length=36)
    battery_level: int | None = Field(default=None, ge=0, le=100)
    fill_level: int | None = Field(default=None, ge=0, le=100)
    lock_state: str | None = None
    reported_at: datetime | None = None


def internal_auth(x_internal_token: str = Header(default="")) -> None:
    token = os.getenv("TELEMETRY_INTERNAL_TOKEN", "")
    if not token or not hmac.compare_digest(x_internal_token, token):
        raise HTTPException(403, "Forbidden")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.getenv("TELEMETRY_INTERNAL_TOKEN"):
        raise RuntimeError("TELEMETRY_INTERNAL_TOKEN is required")
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="ComPost Telemetry", lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return metrics_text()


@app.get("/health")
def health():
    with SessionLocal() as db:
        db.execute(select(ProcessedEvent.event_id).limit(1))
    return {"status": "ok"}


@app.post("/v1/events", dependencies=[Depends(internal_auth)])
def accept_event(event: Event):
    if event.kind not in {"telemetry.recorded", "composter.deleted"}:
        raise HTTPException(422, "Unsupported event kind")
    if event.kind == "telemetry.recorded" and (event.battery_level is None or event.fill_level is None or event.lock_state is None):
        raise HTTPException(422, "Incomplete reading")
    with SessionLocal.begin() as db:
        if db.get(ProcessedEvent, event.id):
            return {"status": "duplicate"}
        if event.kind == "telemetry.recorded":
            db.add(Reading(
                event_id=event.id,
                composter_id=event.composter_id,
                battery_level=event.battery_level,
                fill_level=event.fill_level,
                lock_state=event.lock_state,
                reported_at=event.reported_at or datetime.now(timezone.utc),
            ))
        else:
            db.execute(delete(Reading).where(Reading.composter_id == event.composter_id))
        db.add(ProcessedEvent(event_id=event.id))
    return {"status": "accepted"}


@app.get("/v1/composters/{composter_id}/readings", dependencies=[Depends(internal_auth)])
def readings(composter_id: str, limit: int = 100):
    if limit < 1 or limit > 1000:
        raise HTTPException(422, "Invalid limit")
    with SessionLocal() as db:
        rows = db.scalars(select(Reading).where(Reading.composter_id == composter_id).order_by(Reading.reported_at.desc()).limit(limit)).all()
        return [{"event_id": row.event_id, "battery_level": row.battery_level, "fill_level": row.fill_level, "lock_state": row.lock_state, "reported_at": row.reported_at} for row in rows]
