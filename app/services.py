"""Compatibility facade for domain services.

New code should import from ``app.domain`` modules directly.
"""

from .domain.audit import audit
from .domain.geo import distance_m
from .domain.ml import request_ml_review
from .domain.moderation import apply_violation, reward_review
from .models import Incident, User
from .domain.storage import photo_url, store_photo


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
    audit(db, actor, "incident", incident.id, "INCIDENT_CREATED_FROM_REVIEW", {"review_id": review_id, "composter_id": composter_id})
    return incident

__all__ = [
    "apply_violation",
    "audit",
    "distance_m",
    "create_moderation_incident",
    "incident_title",
    "photo_url",
    "request_ml_review",
    "reward_review",
    "store_photo",
]
