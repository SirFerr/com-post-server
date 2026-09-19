"""Queue legacy telemetry readings for the independent history service."""

import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import OutboxEvent, Telemetry


def main() -> None:
    count = 0
    with SessionLocal.begin() as db:
        for row in db.scalars(select(Telemetry).order_by(Telemetry.reported_at, Telemetry.id)).all():
            if db.get(OutboxEvent, row.id):
                continue
            db.add(OutboxEvent(
                id=row.id,
                destination="telemetry",
                kind="telemetry.recorded",
                payload=json.dumps({
                    "composter_id": row.composter_id,
                    "battery_level": row.battery_level,
                    "fill_level": row.fill_level,
                    "lock_state": row.lock_state,
                    "reported_at": row.reported_at.isoformat(),
                }),
                created_at=row.reported_at,
            ))
            count += 1
    print(f"Queued {count} legacy telemetry readings")


if __name__ == "__main__":
    main()
