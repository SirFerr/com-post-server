import json

from sqlalchemy.orm import Session

from ..models import AuditLog, User


def audit(
    db: Session,
    actor: User | None,
    entity: str,
    entity_id: str,
    action: str,
    details: dict | None = None,
) -> None:
    db.add(AuditLog(
        entity=entity,
        entity_id=entity_id,
        action=action,
        user_id=actor.id if actor else None,
        details=json.dumps(details or {}, ensure_ascii=False),
    ))
