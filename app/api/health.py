"""Liveness, dependency readiness and internal metrics endpoints."""
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select, text

from ..config import get_settings
from ..database import SessionLocal
from ..observability import metrics_text
from ..models import ModelTrainingRun, OutboxEvent, WorkerHeartbeat

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/live")
def health_live():
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready():
    settings = get_settings()
    checks = {}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    for name, endpoint, key, secret, bucket in (
        ("firmware", settings.firmware_s3_endpoint, settings.firmware_s3_access_key, settings.firmware_s3_secret_key, settings.firmware_s3_bucket),
    ):
        try:
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=key,
                aws_secret_access_key=secret,
                config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 0}),
            ).head_bucket(Bucket=bucket)
            checks[name] = "ok"
        except Exception:
            checks[name] = "unavailable"
    try:
        with urllib.request.urlopen(f"{settings.ml_service_url.rstrip('/')}/health", timeout=2) as response:
            checks["ml"] = "ok" if response.status == 200 else "unavailable"
    except Exception:
        checks["ml"] = "unavailable"
    try:
        with urllib.request.urlopen(f"{settings.media_service_url.rstrip('/')}/health/ready", timeout=2) as response:
            checks["photos"] = "ok" if response.status == 200 else "unavailable"
    except Exception:
        checks["photos"] = "unavailable"
    if any(value != "ok" for value in checks.values()):
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ready", "checks": checks}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics():
    with SessionLocal() as db:
        pending = db.scalar(select(func.count(OutboxEvent.id)).where(OutboxEvent.delivered_at.is_(None))) or 0
        statuses = db.execute(select(ModelTrainingRun.status, func.count(ModelTrainingRun.id)).group_by(ModelTrainingRun.status)).all()
        oldest_queued = db.scalar(select(func.min(ModelTrainingRun.created_at)).where(ModelTrainingRun.status == "QUEUED"))
        heartbeats = {row.name: row.last_seen_at for row in db.scalars(select(WorkerHeartbeat)).all()}
    if oldest_queued and oldest_queued.tzinfo is None:
        oldest_queued = oldest_queued.replace(tzinfo=timezone.utc)
    queued_age = max(0, (datetime.now(timezone.utc) - oldest_queued).total_seconds()) if oldest_queued else 0
    lines = [
        "# HELP compost_outbox_pending Undelivered integration events",
        "# TYPE compost_outbox_pending gauge",
        f"compost_outbox_pending {pending}",
        "# HELP compost_training_runs Training runs by status",
        "# TYPE compost_training_runs gauge",
    ]
    lines.extend(f'compost_training_runs{{status="{status}"}} {count}' for status, count in statuses)
    lines.extend([
        "# HELP compost_training_oldest_queued_seconds Age of oldest queued training run",
        "# TYPE compost_training_oldest_queued_seconds gauge",
        f"compost_training_oldest_queued_seconds {queued_age}",
        "# HELP compost_worker_last_seen_timestamp_seconds Last successful worker heartbeat as Unix time",
        "# TYPE compost_worker_last_seen_timestamp_seconds gauge",
    ])
    for name in ("ml-trainer", "telemetry-worker"):
        seen = heartbeats.get(name)
        if seen and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        lines.append(f'compost_worker_last_seen_timestamp_seconds{{name="{name}"}} {seen.timestamp() if seen else 0}')
    return metrics_text() + "\n".join(lines) + "\n"
