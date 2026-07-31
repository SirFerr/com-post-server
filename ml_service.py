import io
import json
import time
from pathlib import Path

import boto3
import numpy as np
import onnxruntime as ort
from PIL import Image
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import get_settings

app = FastAPI(title="ComPost contamination detector")
MODEL_DIR = Path("/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)
runtime = {"session": None, "metadata": None, "checked_at": 0.0}


class AnalyzeRequest(BaseModel):
    photo_key: str


def client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


def refresh_model() -> None:
    if time.monotonic() - runtime["checked_at"] < 30:
        return
    runtime["checked_at"] = time.monotonic()
    settings = get_settings()
    try:
        metadata = json.loads(
            client().get_object(Bucket=settings.s3_bucket, Key="ml/active/active.json")["Body"].read()
        )
    except Exception:
        return
    if runtime["metadata"] == metadata and runtime["session"] is not None:
        return
    model_path = MODEL_DIR / f"{metadata['run_id']}.onnx"
    client().download_file(settings.s3_bucket, "ml/active/model.onnx", str(model_path))
    runtime["session"] = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    runtime["metadata"] = metadata


def prepare(image: Image.Image, size: int = 640) -> np.ndarray:
    image = image.convert("RGB")
    image.thumbnail((size, size))
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    array = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return array[None]


def trained_prediction(image: Image.Image) -> dict | None:
    refresh_model()
    session = runtime["session"]
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
            "model": f"trained-{runtime['metadata']['run_id']}",
        }
    return {
        "status": "LIKELY_VALID",
        "confidence": round(1 - confidence, 4),
        "violations": [],
        "model": f"trained-{runtime['metadata']['run_id']}",
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
        return {"status": "LIKELY_INVALID", "confidence": min(0.95, 0.55 + blue_ratio), "violations": ["POSSIBLE_PLASTIC"], "model": "color-heuristic-v1"}
    if organic_ratio > 0.58:
        return {"status": "LIKELY_VALID", "confidence": min(0.9, organic_ratio), "violations": [], "model": "color-heuristic-v1"}
    return {"status": "NEEDS_MANUAL_REVIEW", "confidence": round(abs(organic_ratio - 0.5), 3), "violations": [], "model": "color-heuristic-v1"}


@app.get("/health")
def health():
    refresh_model()
    return {
        "status": "ok",
        "model": f"trained-{runtime['metadata']['run_id']}" if runtime["metadata"] else "color-heuristic-v1",
        "trained_model_active": runtime["metadata"] is not None,
    }


@app.post("/analyze")
def analyze(request: AnalyzeRequest):
    settings = get_settings()
    try:
        payload = client().get_object(Bucket=settings.s3_bucket, Key=request.photo_key)["Body"].read()
        image = Image.open(io.BytesIO(payload))
        return trained_prediction(image) or heuristic_prediction(image)
    except Exception as exc:
        raise HTTPException(422, f"Cannot analyze image: {exc}")
