import numpy as np
from PIL import Image

from ml_runtime.preprocessing import prepare


def trained_prediction(image: Image.Image) -> dict | None:
    from ml_runtime.registry import registry

    registry.refresh()
    session = registry.session
    if session is None:
        return None
    input_name = session.get_inputs()[0].name
    output = np.asarray(session.run(None, {input_name: prepare(image)})[0])
    detections = output[0] if output.ndim == 3 else output
    if detections.ndim != 2 or detections.shape[1] < 5:
        raise RuntimeError(f"Unexpected ONNX output shape: {output.shape}")
    scores = detections[:, 4]
    scores = scores[np.isfinite(scores)]
    confidence = float(scores.max()) if scores.size else 0.0
    if confidence >= 0.35:
        return {
            "status": "LIKELY_INVALID",
            "confidence": round(confidence, 4),
            "violations": ["CONTAMINATION"],
            "model": registry.model_name,
        }
    return {
        "status": "LIKELY_VALID",
        "confidence": round(1 - confidence, 4),
        "violations": [],
        "model": registry.model_name,
    }


def heuristic_prediction(image: Image.Image) -> dict:
    pixels = np.asarray(image.convert("RGB").resize((224, 224)), dtype=np.float32) / 255
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    organic = ((green > red * 0.85) & (green > blue * 1.05)) | (
        (red > green * 0.9) & (green > blue * 1.15)
    )
    suspicious_blue = (blue > red * 1.25) & (blue > green * 1.2)
    organic_ratio, blue_ratio = float(organic.mean()), float(suspicious_blue.mean())
    if blue_ratio > 0.18:
        return {
            "status": "LIKELY_INVALID",
            "confidence": min(0.95, 0.55 + blue_ratio),
            "violations": ["POSSIBLE_PLASTIC"],
            "model": "color-heuristic-v1",
        }
    if organic_ratio > 0.58:
        return {
            "status": "LIKELY_VALID",
            "confidence": min(0.9, organic_ratio),
            "violations": [],
            "model": "color-heuristic-v1",
        }
    return {
        "status": "NEEDS_MANUAL_REVIEW",
        "confidence": round(abs(organic_ratio - 0.5), 3),
        "violations": [],
        "model": "color-heuristic-v1",
    }
