import io

import boto3
import numpy as np
from PIL import Image
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import get_settings

app = FastAPI(title="Compost photo baseline classifier")


class AnalyzeRequest(BaseModel):
    photo_key: str


@app.get("/health")
def health():
    return {"status": "ok", "model": "color-heuristic-v1"}


@app.post("/analyze")
def analyze(request: AnalyzeRequest):
    settings = get_settings()
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    try:
        payload = client.get_object(Bucket=settings.s3_bucket, Key=request.photo_key)["Body"].read()
        image = np.asarray(Image.open(io.BytesIO(payload)).convert("RGB").resize((224, 224)), dtype=np.float32) / 255
    except Exception as exc:
        raise HTTPException(422, f"Cannot decode image: {exc}")
    red, green, blue = image[..., 0], image[..., 1], image[..., 2]
    organic = ((green > red * 0.85) & (green > blue * 1.05)) | ((red > green * 0.9) & (green > blue * 1.15))
    suspicious_blue = (blue > red * 1.25) & (blue > green * 1.2)
    organic_ratio, blue_ratio = float(organic.mean()), float(suspicious_blue.mean())
    if blue_ratio > 0.18:
        return {"status": "LIKELY_INVALID", "confidence": min(0.95, 0.55 + blue_ratio), "violations": ["POSSIBLE_PLASTIC"]}
    if organic_ratio > 0.58:
        return {"status": "LIKELY_VALID", "confidence": min(0.9, organic_ratio), "violations": []}
    return {"status": "NEEDS_MANUAL_REVIEW", "confidence": round(abs(organic_ratio - 0.5), 3), "violations": []}
