import json
import math
import uuid

import boto3
import httpx
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AccessSession, AuditLog, Review, User, UserScore, Violation


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def store_photo(file: UploadFile) -> str:
    settings = get_settings()
    key = f"deposits/{uuid.uuid4()}-{file.filename or 'photo.jpg'}"
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    client.upload_fileobj(file.file, settings.s3_bucket, key, ExtraArgs={"ContentType": file.content_type or "image/jpeg"})
    return key


def photo_url(key: str) -> str:
    settings = get_settings()
    client = boto3.client("s3", endpoint_url=settings.s3_public_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    return client.generate_presigned_url("get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=900)


def request_ml_review(photo_key: str) -> dict:
    try:
        response = httpx.post(f"{get_settings().ml_service_url}/analyze", json={"photo_key": photo_key}, timeout=5)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return {"status": "NEEDS_MANUAL_REVIEW", "confidence": 0.0, "violations": []}


def apply_violation(db: Session, review: Review, session: AccessSession, reason: str) -> None:
    existing = db.scalar(select(Violation).where(Violation.review_id == review.id))
    if not existing:
        db.add(Violation(user_id=session.user_id, review_id=review.id, reason=reason))
        db.flush()
    active = db.scalar(select(func.count(Violation.id)).where(Violation.user_id == session.user_id, Violation.is_active.is_(True))) or 0
    if active >= 3:
        user = db.get(User, session.user_id)
        user.is_blocked = True
        user.ban_reason = "Three active composting violations"


def audit(db: Session, actor: User | None, entity: str, entity_id: str, action: str, details: dict | None = None) -> None:
    db.add(AuditLog(entity=entity, entity_id=entity_id, action=action, user_id=actor.id if actor else None, details=json.dumps(details or {}, ensure_ascii=False)))


def reward_review(db: Session, session: AccessSession, approved: bool) -> None:
    score = db.get(UserScore, session.user_id)
    if not score:
        score = UserScore(user_id=session.user_id, points=0, total_uploads=0, valid_uploads=0)
        db.add(score)
    score.total_uploads += 1
    if approved:
        score.valid_uploads += 1
        score.points += 10
    else:
        score.points = max(0, score.points - 15)
