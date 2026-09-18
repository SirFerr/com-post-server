"""Incident-backed equipment state transitions."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import audit
from ..models import Composter, Incident, User


ACTIVE_INCIDENT_STATUSES = ("OPEN", "IN_PROGRESS")


def audit_incident_resolved(
    db: Session,
    actor: User,
    incident: Incident,
    source: str,
) -> None:
    details = {
        "incident_id": incident.id,
        "composter_id": incident.composter_id,
        "kind": incident.kind,
        "source": source,
    }
    audit(db, actor, "incident", incident.id, "INCIDENT_RESOLVED", details)
    if incident.composter_id:
        audit(db, actor, "composter", incident.composter_id, "INCIDENT_RESOLVED", details)
    if incident.created_by:
        audit(db, actor, "user", incident.created_by, "INCIDENT_RESOLVED", details)


def set_full_state_from_incident(
    db: Session,
    actor: User,
    composter: Composter,
    needs_emptying: bool,
) -> Incident | None:
    """Represent the manual full flag by an active OVERFLOW incident."""
    active = list(db.scalars(
        select(Incident)
        .where(
            Incident.composter_id == composter.id,
            Incident.kind == "OVERFLOW",
            Incident.status.in_(ACTIVE_INCIDENT_STATUSES),
        )
        .with_for_update()
    ).all())

    if needs_emptying:
        incident = active[0] if active else Incident(
            composter_id=composter.id,
            kind="OVERFLOW",
            severity="MEDIUM",
            title="Переполнение",
            description="Компостер отмечен как заполненный",
            created_by=actor.id,
        )
        if not active:
            db.add(incident)
            db.flush()
            creation_details = {
                "composter_id": composter.id,
                "kind": "OVERFLOW",
                "source": "FULL_STATE",
                "incident_id": incident.id,
            }
            audit(db, actor, "incident", incident.id, "INCIDENT_CREATED", creation_details)
            audit(db, actor, "composter", composter.id, "INCIDENT_CREATED", creation_details)
        composter.needs_emptying = True
        audit(db, actor, "composter", composter.id, "FULL_REPORTED", {"incident_id": incident.id})
        return incident

    now = datetime.now(timezone.utc)
    for incident in active:
        incident.status = "RESOLVED"
        incident.resolved_by = actor.id
        incident.resolved_at = now
        audit_incident_resolved(db, actor, incident, "FULL_STATE")
    composter.needs_emptying = False
    audit(db, actor, "composter", composter.id, "FULL_REPORT_CLEARED", {
        "incident_ids": [incident.id for incident in active],
    })
    return None


def sync_full_state_after_incident_change(db: Session, composter_id: str | None) -> None:
    """Keep the compatibility flag derived from active OVERFLOW incidents."""
    if not composter_id:
        return
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        return
    active = db.scalar(
        select(Incident.id)
        .where(
            Incident.composter_id == composter_id,
            Incident.kind == "OVERFLOW",
            Incident.status.in_(ACTIVE_INCIDENT_STATUSES),
        )
        .limit(1)
    )
    composter.needs_emptying = active is not None


INCIDENT_TITLES = {
    "CONTAMINATION": "Загрязнение",
    "OVERFLOW": "Переполнение",
    "LOCK": "Проблема с замком",
    "DEVICE": "Неисправность устройства",
    "MAINTENANCE": "Требуется обслуживание",
    "USER_REPORT": "Сообщение пользователя",
    "OTHER": "Инцидент",
}


def incident_title(kind: str) -> str:
    return INCIDENT_TITLES.get(kind.strip().upper(), "Инцидент")


MODERATION_INCIDENT_KINDS = frozenset(INCIDENT_TITLES) - {"USER_REPORT", "CONTAMINATION"}


def create_moderation_incident(db, actor: User, composter_id: str, kind: str | None, comment: str | None, review_id: str, photo_key: str | None = None) -> Incident | None:
    normalized_kind = (kind or "").strip().upper()
    if not normalized_kind:
        return None
    if normalized_kind not in MODERATION_INCIDENT_KINDS:
        raise ValueError("Unknown incident type")
    incident = Incident(
        composter_id=composter_id,
        kind=normalized_kind,
        severity="MEDIUM",
        title=incident_title(normalized_kind),
        description=(comment or "").strip(),
        photo_key=photo_key,
        created_by=actor.id,
    )
    db.add(incident)
    db.flush()
    if incident.kind == "OVERFLOW":
        composter = db.get(Composter, composter_id)
        if composter:
            composter.needs_emptying = True
    audit(db, actor, "incident", incident.id, "INCIDENT_CREATED_FROM_REVIEW", {"review_id": review_id, "composter_id": composter_id})
    return incident
