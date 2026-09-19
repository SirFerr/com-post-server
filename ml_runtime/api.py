import io
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from PIL import Image
from pydantic import BaseModel, Field

from ml_runtime.inference import heuristic_prediction, trained_prediction
from ml_runtime.registry import registry
from ml_runtime.storage import read_photo
from app.config import get_settings
from app.observability import ObservabilityMiddleware, metrics_text

app = FastAPI(title="ComPost contamination detector")
app.add_middleware(ObservabilityMiddleware)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return metrics_text()


class AnalyzeRequest(BaseModel):
    photo_key: str = Field(min_length=1, max_length=512)


@app.get("/health")
def health():
    registry.refresh()
    return {
        "status": "ok",
        "model": registry.model_name,
        "trained_model_active": registry.metadata is not None,
    }


@app.post("/analyze")
def analyze(request: AnalyzeRequest):
    try:
        with Image.open(io.BytesIO(read_photo(request.photo_key))) as image:
            if image.width * image.height > get_settings().max_image_pixels:
                raise ValueError("Image resolution exceeds the allowed limit")
            return trained_prediction(image) or heuristic_prediction(image)
    except Exception as exc:
        logging.getLogger(__name__).exception("Image analysis failed")
        raise HTTPException(422, "Cannot analyze image") from exc
