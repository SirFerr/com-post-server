"""Compatibility facade for domain services.

New code should import from ``app.domain`` modules directly.
"""

from .domain.audit import audit
from .domain.geo import distance_m
from .domain.ml import request_ml_review
from .domain.moderation import apply_violation, reward_review
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

__all__ = [
    "apply_violation",
    "audit",
    "distance_m",
    "incident_title",
    "photo_url",
    "request_ml_review",
    "reward_review",
    "store_photo",
]
