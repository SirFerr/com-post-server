"""Periodic database heartbeat for background workers."""

import logging
import threading
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import WorkerHeartbeat


logger = logging.getLogger(__name__)


def write_heartbeat(name: str) -> None:
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, name)
        if row is None:
            db.add(WorkerHeartbeat(name=name, last_seen_at=datetime.now(timezone.utc)))
        else:
            row.last_seen_at = datetime.now(timezone.utc)


def start_heartbeat(name: str, interval_seconds: int = 15) -> None:
    sleeper = threading.Event()

    def run() -> None:
        while True:
            try:
                write_heartbeat(name)
            except Exception:
                logger.exception("Worker heartbeat failed: %s", name)
            sleeper.wait(interval_seconds)

    threading.Thread(target=run, name=f"heartbeat-{name}", daemon=True).start()
