"""Deliver committed telemetry outbox events to the independent history service."""

import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import OutboxEvent
from tools.worker_heartbeat import start_heartbeat


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def deliver_once() -> int:
    url = os.environ["TELEMETRY_SERVICE_URL"].rstrip("/") + "/v1/events"
    token = os.environ["TELEMETRY_INTERNAL_TOKEN"]
    delivered = 0
    with SessionLocal.begin() as db:
        rows = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.destination == "telemetry", OutboxEvent.delivered_at.is_(None))
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        with httpx.Client(timeout=5) as client:
            for row in rows:
                response = client.post(url, json={"id": row.id, "kind": row.kind, **json.loads(row.payload)}, headers={"X-Internal-Token": token})
                response.raise_for_status()
                row.delivered_at = datetime.now(timezone.utc)
                delivered += 1
    return delivered


def main() -> None:
    start_heartbeat("telemetry-worker")
    while True:
        try:
            if not deliver_once():
                time.sleep(2)
        except Exception:
            logger.exception("Telemetry delivery failed; retrying")
            time.sleep(5)


if __name__ == "__main__":
    main()
