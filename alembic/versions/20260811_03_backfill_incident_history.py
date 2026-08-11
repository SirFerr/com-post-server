"""Backfill resolved incident history for composters and reporters."""

import json
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260811_03"
down_revision = "20260811_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("incidents") or not inspector.has_table("audit_logs"):
        return

    metadata = sa.MetaData()
    incidents = sa.Table("incidents", metadata, autoload_with=bind)
    audit_logs = sa.Table("audit_logs", metadata, autoload_with=bind)
    resolved = bind.execute(
        sa.select(
            incidents.c.id,
            incidents.c.composter_id,
            incidents.c.kind,
            incidents.c.created_by,
            incidents.c.resolved_by,
            incidents.c.resolved_at,
        ).where(incidents.c.status == "RESOLVED")
    ).mappings().all()
    existing_rows = bind.execute(
        sa.select(audit_logs.c.entity, audit_logs.c.entity_id, audit_logs.c.details)
        .where(audit_logs.c.action == "INCIDENT_RESOLVED")
    ).mappings().all()
    existing: set[tuple[str, str, str]] = set()
    for row in existing_rows:
        try:
            incident_id = str(json.loads(row["details"] or "{}").get("incident_id") or "")
        except (TypeError, json.JSONDecodeError):
            incident_id = ""
        if incident_id:
            existing.add((str(row["entity"]), str(row["entity_id"]), incident_id))

    inserts = []
    for incident in resolved:
        incident_id = str(incident["id"])
        details = json.dumps({
            "incident_id": incident_id,
            "composter_id": incident["composter_id"],
            "kind": incident["kind"],
            "source": "MIGRATION_BACKFILL",
        }, ensure_ascii=False)
        targets = [("incident", incident_id)]
        if incident["composter_id"]:
            targets.append(("composter", str(incident["composter_id"])))
        if incident["created_by"]:
            targets.append(("user", str(incident["created_by"])))
        for entity, entity_id in targets:
            if (entity, entity_id, incident_id) in existing:
                continue
            inserts.append({
                "id": str(uuid.uuid4()),
                "entity": entity,
                "entity_id": entity_id,
                "action": "INCIDENT_RESOLVED",
                "user_id": incident["resolved_by"],
                "details": details,
                "created_at": incident["resolved_at"] or datetime.now(timezone.utc),
            })
    if inserts:
        bind.execute(audit_logs.insert(), inserts)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("audit_logs"):
        return
    metadata = sa.MetaData()
    audit_logs = sa.Table("audit_logs", metadata, autoload_with=bind)
    bind.execute(
        audit_logs.delete().where(
            audit_logs.c.action == "INCIDENT_RESOLVED",
            audit_logs.c.details.like('%"source": "MIGRATION_BACKFILL"%'),
        )
    )
