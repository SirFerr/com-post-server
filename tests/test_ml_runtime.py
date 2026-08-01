import numpy as np
from PIL import Image

from ml_runtime.inference import heuristic_prediction
from ml_runtime.preprocessing import prepare


def test_prepare_returns_normalized_batched_chw_tensor():
    tensor = prepare(Image.new("RGB", (320, 160), (255, 0, 0)))

    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert 0.0 <= float(tensor.min()) <= float(tensor.max()) <= 1.0


def test_blue_image_is_rejected_by_baseline_heuristic():
    result = heuristic_prediction(Image.new("RGB", (64, 64), (0, 0, 255)))

    assert result["status"] == "LIKELY_INVALID"
    assert result["violations"] == ["POSSIBLE_PLASTIC"]
    assert result["model"] == "color-heuristic-v1"
