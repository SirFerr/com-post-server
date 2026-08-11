from app.ml_dataset import require_private_training_manifest, split_by_composter, training_readiness
import pytest


def test_composters_never_cross_dataset_splits():
    assignments = split_by_composter([f"composter-{index}" for index in range(10)])
    assert set(assignments.values()) == {"train", "val", "test"}
    assert assignments == split_by_composter(list(reversed(list(assignments))))


def test_training_readiness_requires_volume_boxes_and_sites():
    manifest = {
        "schema_version": 5,
        "selection_policy": "admin-selected-reviewed-v1",
        "privacy_policy": "minimized-reviewed-photos-v1",
        "samples": [],
        "summary": {
            "samples": 200,
            "annotated_contamination": 50,
            "sources": 3,
        }
    }
    assert training_readiness(manifest)["ready"] is True
    assert training_readiness(manifest)["trainable"] is True
    manifest["summary"]["sources"] = 2
    assert training_readiness(manifest)["ready"] is False
    manifest["summary"].update(samples=6, annotated_contamination=2)
    assert training_readiness(manifest)["trainable"] is True
    manifest["summary"]["annotated_contamination"] = 1
    assert training_readiness(manifest)["trainable"] is False


def test_deployment_requires_balanced_independent_test_set():
    samples = [
        {"source": "manual", "user_id": None, "split": "test", "label": label}
        for label in ("clean", "contamination")
        for _ in range(20)
    ]
    manifest = {
        "schema_version": 5,
        "selection_policy": "admin-selected-reviewed-v1",
        "privacy_policy": "minimized-reviewed-photos-v1",
        "samples": samples,
        "summary": {"samples": 200, "annotated_contamination": 50, "sources": 3, "splits": {"test": 40}},
    }
    assert training_readiness(manifest)["deployable"] is True
    manifest["samples"].pop()
    assert training_readiness(manifest)["deployable"] is False


def test_legacy_or_user_photo_manifests_are_blocked():
    legacy = {"schema_version": 3, "summary": {"samples": 200, "annotated_contamination": 50, "sources": 3}, "samples": []}
    assert training_readiness(legacy)["privacy_safe"] is False
    with pytest.raises(ValueError):
        require_private_training_manifest(legacy)

    user_photo = {
        "schema_version": 5,
        "selection_policy": "admin-selected-reviewed-v1",
        "privacy_policy": "minimized-reviewed-photos-v1",
        "summary": {"samples": 200, "annotated_contamination": 50, "sources": 3},
        "samples": [{"source": "moderation", "user_id": "user-1"}],
    }
    with pytest.raises(ValueError):
        require_private_training_manifest(user_photo)
