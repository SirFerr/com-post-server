import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AccessSession, DatasetVersion, Review, ReviewStatus


DATASET_SCHEMA_VERSION = 2


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def split_by_composter(composter_ids: list[str]) -> dict[str, str]:
    """Assign whole composters to stable splits so near-duplicate scenes cannot leak."""
    groups = sorted(set(composter_ids), key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    count = len(groups)
    if count == 1:
        return {groups[0]: "train"}
    if count == 2:
        return {groups[0]: "train", groups[1]: "val"}

    test_count = max(1, round(count * 0.15))
    val_count = max(1, round(count * 0.15))
    while test_count + val_count >= count:
        if test_count >= val_count:
            test_count -= 1
        else:
            val_count -= 1
    train_end = count - val_count - test_count
    val_end = count - test_count
    return {
        group: "train" if index < train_end else "val" if index < val_end else "test"
        for index, group in enumerate(groups)
    }


def build_dataset_manifest(db: Session) -> dict:
    rows = db.execute(
        select(Review, AccessSession)
        .join(AccessSession, Review.session_id == AccessSession.id)
        .where(Review.status != ReviewStatus.PENDING)
        .order_by(Review.created_at, Review.id)
    ).all()
    assignments = split_by_composter([session.composter_id for _, session in rows]) if rows else {}
    samples = []
    for review, session in rows:
        boxes = json.loads(review.annotations or "[]")
        samples.append({
            "review_id": review.id,
            "photo_key": review.photo_key,
            "label": "clean" if review.status == ReviewStatus.APPROVED else "contamination",
            "moderation_status": review.status.value,
            "violation": review.violation_reason,
            "boxes": boxes,
            "split": assignments[session.composter_id],
            "composter_id": session.composter_id,
            "user_id": session.user_id,
            "captured_at": _iso(review.created_at),
            "reviewed_at": _iso(review.reviewed_at),
        })

    labels = Counter(sample["label"] for sample in samples)
    splits = Counter(sample["split"] for sample in samples)
    annotated = sum(bool(sample["boxes"]) for sample in samples)
    annotated_contamination = sum(
        sample["label"] == "contamination" and bool(sample["boxes"]) for sample in samples
    )
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classes": ["contamination"],
        "split_strategy": "grouped-by-composter-sha256-v1",
        "summary": {
            "samples": len(samples),
            "annotated": annotated,
            "annotated_contamination": annotated_contamination,
            "composters": len(assignments),
            "labels": dict(labels),
            "splits": dict(splits),
        },
        "samples": samples,
    }


def training_readiness(manifest: dict) -> dict:
    summary = manifest["summary"]
    checks = {
        "samples": {"actual": summary["samples"], "recommended": 200},
        "contamination_boxes": {
            "actual": summary["annotated_contamination"],
            "recommended": 50,
        },
        "composters": {"actual": summary["composters"], "recommended": 3},
    }
    return {
        "ready": all(item["actual"] >= item["recommended"] for item in checks.values()),
        "checks": checks,
    }


def freeze_dataset_version(db: Session, created_by: str) -> DatasetVersion:
    manifest = build_dataset_manifest(db)
    summary = manifest["summary"]
    version = (db.scalar(select(func.max(DatasetVersion.version))) or 0) + 1
    row = DatasetVersion(
        version=version,
        sample_count=summary["samples"],
        annotated_count=summary["annotated"],
        manifest=json.dumps(manifest, ensure_ascii=False),
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    return row
