"""Liveness, dependency readiness and internal metrics endpoints."""
import urllib.request

import boto3
from botocore.config import Config
from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from ..config import get_settings
from ..database import SessionLocal
from ..observability import metrics_text

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
        ("photos", settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_bucket),
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
    if any(value != "ok" for value in checks.values()):
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ready", "checks": checks}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return metrics_text()
