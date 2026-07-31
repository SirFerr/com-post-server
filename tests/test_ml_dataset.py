from app.ml_dataset import split_by_composter, training_readiness


def test_composters_never_cross_dataset_splits():
    assignments = split_by_composter([f"composter-{index}" for index in range(10)])
    assert set(assignments.values()) == {"train", "val", "test"}
    assert assignments == split_by_composter(list(reversed(list(assignments))))


def test_training_readiness_requires_volume_boxes_and_sites():
    manifest = {
        "summary": {
            "samples": 200,
            "annotated_contamination": 50,
            "composters": 3,
        }
    }
    assert training_readiness(manifest)["ready"] is True
    manifest["summary"]["composters"] = 2
    assert training_readiness(manifest)["ready"] is False
