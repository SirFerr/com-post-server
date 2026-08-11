import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AccessSession, DatasetVersion, MLDatasetPhotoArchive, MLDatasetSample, MLDatasetSampleArchive, Review, ReviewStatus


DATASET_SCHEMA_VERSION = 5


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def split_by_composter(composter_ids: list[str]) -> dict[str, str]:
    """Assign whole sources to stable splits so related scenes cannot leak."""
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


def build_dataset_manifest(db: Session, sample_ids: list[str] | None = None) -> dict:
    legacy_archived_ids = {f"manual:{value}" for value in db.scalars(select(MLDatasetSampleArchive.sample_id)).all()}
    archived_ids = legacy_archived_ids | set(db.scalars(select(MLDatasetPhotoArchive.source_key)).all())
    requested_ids = set(sample_ids) if sample_ids is not None else None
    if requested_ids is not None:
        requested_ids = {value if ":" in value else f"manual:{value}" for value in requested_ids}
    manual_rows = list(db.scalars(select(MLDatasetSample).order_by(MLDatasetSample.created_at, MLDatasetSample.id)).all())
    manual_rows = [
        sample for sample in manual_rows
        if f"manual:{sample.id}" not in archived_ids and (requested_ids is None or f"manual:{sample.id}" in requested_ids)
    ]
    review_rows = list(db.execute(
        select(Review, AccessSession)
        .join(AccessSession, Review.session_id == AccessSession.id)
        .where(Review.status != ReviewStatus.PENDING)
        .order_by(Review.created_at, Review.id)
    ).all())
    review_rows = [
        (review, session) for review, session in review_rows
        if f"review:{review.id}" not in archived_ids and (requested_ids is None or f"review:{review.id}" in requested_ids)
    ]
    if requested_ids is not None:
        selected_ids = {f"manual:{sample.id}" for sample in manual_rows} | {f"review:{review.id}" for review, _ in review_rows}
        unavailable = requested_ids - selected_ids
        if unavailable:
            raise ValueError("Some selected photos are missing, pending moderation, or archived")
        if not manual_rows and not review_rows:
            raise ValueError("Select at least one ML photo")
    manual_groups = [f"manual:{sample.source_group}" for sample in manual_rows]
    review_groups = [f"composter:{session.composter_id}" for _, session in review_rows]
    all_groups = manual_groups + review_groups
    assignments = split_by_composter(all_groups) if all_groups else {}
    samples = []
    for sample in manual_rows:
        group = f"manual:{sample.source_group}"
        samples.append({
            "review_id": f"manual:{sample.id}",
            "photo_key": sample.photo_key,
            "label": sample.label.lower(),
            "moderation_status": None,
            "violation": "CONTAMINATION" if sample.label == "CONTAMINATION" else None,
            "boxes": json.loads(sample.annotations or "[]"),
            "split": assignments[group],
            "source": "manual",
            "source_group": group,
            "composter_id": None,
            "user_id": None,
            "captured_at": _iso(sample.created_at),
            "reviewed_at": _iso(sample.created_at),
        })
    for review, session in review_rows:
        group = f"composter:{session.composter_id}"
        contaminated = review.status == ReviewStatus.REJECTED
        samples.append({
            "review_id": f"review:{review.id}",
            "photo_key": review.photo_key,
            "label": "contamination" if contaminated else "clean",
            "moderation_status": review.status.value,
            "violation": review.violation_reason if contaminated else None,
            "boxes": json.loads(review.annotations or "[]"),
            "split": assignments[group],
            "source": "moderation",
            "source_group": group,
            "composter_id": session.composter_id,
            "user_id": None,
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
        "selection_policy": "admin-selected-reviewed-v1",
        "privacy_policy": "minimized-reviewed-photos-v1",
        "split_strategy": "grouped-by-source-sha256-v4",
        "summary": {
            "samples": len(samples),
            "annotated": annotated,
            "annotated_contamination": annotated_contamination,
            "sources": len(assignments),
            "manual_samples": len(manual_rows),
            "moderated_samples": len(review_rows),
            "labels": dict(labels),
            "splits": dict(splits),
        },
        "samples": samples,
    }


def training_readiness(manifest: dict) -> dict:
    summary = manifest["summary"]
    privacy_safe = (
        manifest.get("schema_version", 0) >= 5
        and manifest.get("selection_policy") == "admin-selected-reviewed-v1"
        and manifest.get("privacy_policy") == "minimized-reviewed-photos-v1"
        and all(
            sample.get("source") in {"manual", "moderation"}
            and sample.get("user_id") is None
            and (sample.get("source") != "moderation" or sample.get("moderation_status") in {"APPROVED", "REJECTED"})
            for sample in manifest.get("samples", [])
        )
    )
    checks = {
        "samples": {"actual": summary["samples"], "minimum": 6, "recommended": 200},
        "contamination_boxes": {
            "actual": summary["annotated_contamination"],
            "minimum": 2,
            "recommended": 50,
        },
        "sources": {"actual": summary.get("sources", 0), "minimum": 2, "recommended": 3},
    }
    trainable = privacy_safe and all(item["actual"] >= item["minimum"] for item in checks.values())
    test_samples = summary.get("splits", {}).get("test", 0)
    test_clean = sum(sample.get("split") == "test" and sample.get("label") == "clean" for sample in manifest.get("samples", []))
    test_contamination = sum(sample.get("split") == "test" and sample.get("label") == "contamination" for sample in manifest.get("samples", []))
    deployable = (
        trainable
        and checks["sources"]["actual"] >= 3
        and test_clean >= 20
        and test_contamination >= 20
    )
    return {
        "privacy_safe": privacy_safe,
        "trainable": trainable,
        "deployable": deployable,
        "test_samples": test_samples,
        "test_clean": test_clean,
        "test_contamination": test_contamination,
        "ready": privacy_safe and all(item["actual"] >= item["recommended"] for item in checks.values()),
        "checks": checks,
    }


def require_private_training_manifest(manifest: dict) -> None:
    if not training_readiness(manifest)["privacy_safe"]:
        raise ValueError("Training dataset contains unreviewed, legacy, or non-minimized photos")


def freeze_dataset_version(db: Session, created_by: str, sample_ids: list[str] | None = None) -> DatasetVersion:
    manifest = build_dataset_manifest(db, sample_ids)
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
