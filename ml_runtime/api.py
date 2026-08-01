import io

from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

from ml_runtime.inference import heuristic_prediction, trained_prediction
from ml_runtime.registry import registry
from ml_runtime.storage import read_photo

app = FastAPI(title="ComPost contamination detector")


class AnalyzeRequest(BaseModel):
    photo_key: str


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
        image = Image.open(io.BytesIO(read_photo(request.photo_key)))
        return trained_prediction(image) or heuristic_prediction(image)
    except Exception as exc:
        raise HTTPException(422, f"Cannot analyze image: {exc}")
