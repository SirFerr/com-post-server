"""Compatibility facade for domain services.

New code should import from ``app.domain`` modules directly.
"""

from .domain.audit import audit
from .domain.geo import distance_m
from .domain.ml import request_ml_review
from .domain.moderation import apply_violation, reward_review
from .domain.incidents import create_moderation_incident, incident_title
from .domain.storage import photo_url, store_photo


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
