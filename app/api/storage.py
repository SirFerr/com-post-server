import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..domain.media_client import storage_client
from ..models import Review, Role, StorageObjectAnnotation, User
from ..security import require_roles
from ..services import photo_url

router = APIRouter(prefix="/admin/storage", tags=["storage"])


def object_annotations(db: Session, object_key: str) -> list[dict]:
    stored = db.get(StorageObjectAnnotation, object_key)
    if stored:
        return json.loads(stored.annotations or "[]")
    reviews = db.scalars(select(Review).where(Review.photo_key == object_key).order_by(Review.created_at.desc())).all()
    for review in reviews:
        boxes = json.loads(review.annotations or "[]")
        if boxes:
            return boxes
    return []


@router.get("")
def storage_objects(
    response: Response,
    q: str = Query("", max_length=256),
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None, max_length=1024),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR)),
):
    settings = get_settings()
    try:
        page = storage_client().list_objects_v2(
            Bucket=settings.s3_bucket,
            Prefix=q.strip(),
            MaxKeys=limit,
            **({"ContinuationToken": cursor} if cursor else {}),
        )
        objects = page.get("Contents", [])
        if page.get("NextContinuationToken"):
            response.headers["X-Next-Cursor"] = page["NextContinuationToken"]
    except Exception as exc:
        raise HTTPException(503, "S3 storage is unavailable") from exc
    result = [
        {
            "key": item["Key"],
            "name": Path(item["Key"]).name,
            "size": item["Size"],
            "modified": item["LastModified"],
            "url": photo_url(item["Key"]),
        }
        for item in objects
        if item["Size"] >= 128
    ]
    result.sort(key=lambda item: item["modified"], reverse=True)
    return result


@router.get("/object/{object_key:path}")
def storage_object(
    object_key: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR)),
):
    settings = get_settings()
    try:
        head = storage_client().head_object(Bucket=settings.s3_bucket, Key=object_key)
    except Exception as exc:
        raise HTTPException(404, "Storage object not found") from exc
    return {
        "key": object_key,
        "name": Path(object_key).name,
        "size": head.get("ContentLength", 0),
        "modified": head.get("LastModified"),
        "content_type": head.get("ContentType", "application/octet-stream"),
        "etag": str(head.get("ETag", "")).strip('"'),
        "url": photo_url(object_key),
        "annotations": object_annotations(db, object_key),
    }
