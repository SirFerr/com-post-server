import json

import pytest
from PIL import Image

from tools.taco_pretrain import _safe_relative_path, prepare_taco_dataset


def test_prepare_taco_dataset_maps_all_categories_to_contamination(tmp_path):
    images_root = tmp_path / "images"
    images = []
    annotations = []
    for image_id in range(40):
        file_name = f"batch_1/{image_id:06d}.jpg"
        source = images_root / file_name
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 50), (20, 40, 60)).save(source)
        images.append({"id": image_id, "file_name": file_name, "width": 100, "height": 50})
        annotations.append({"id": image_id, "image_id": image_id, "category_id": 99, "bbox": [10, 5, 20, 10]})
    annotations_path = tmp_path / "annotations.json"
    annotations_path.write_text(
        json.dumps({"images": images, "annotations": annotations, "categories": [{"id": 99, "name": "Plastic bag", "supercategory": "Plastic bag & wrapper"}], "licenses": []}),
        encoding="utf-8",
    )

    output = tmp_path / "yolo"
    metadata = prepare_taco_dataset(annotations_path, images_root, output)

    assert metadata["mapping"] == "clearly non-compostable TACO categories -> contamination"
    assert metadata["exported"]["train"] + metadata["exported"]["val"] == 40
    assert metadata["exported"]["train"] > 0
    assert metadata["exported"]["val"] > 0
    label = next((output / "labels").glob("*/*.txt")).read_text(encoding="utf-8")
    assert label == "0 0.20000000 0.20000000 0.20000000 0.20000000"
    assert "0: contamination" in (output / "data.yaml").read_text(encoding="utf-8")


def test_taco_paths_cannot_escape_source_directory():
    with pytest.raises(ValueError):
        _safe_relative_path("../outside.jpg")
