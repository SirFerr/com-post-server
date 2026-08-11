import json
import time
from pathlib import Path
from typing import Any

import onnxruntime as ort

from app.config import get_settings
from ml_runtime.storage import s3_client

MODEL_DIR = Path.home() / ".cache" / "compost" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_REFRESH_SECONDS = 30


class ModelRegistry:
    def __init__(self) -> None:
        self.session: ort.InferenceSession | None = None
        self.metadata: dict[str, Any] | None = None
        self.checked_at = 0.0

    @property
    def model_name(self) -> str:
        return f"trained-{self.metadata['run_id']}" if self.metadata else "color-heuristic-v1"

    def refresh(self) -> None:
        if time.monotonic() - self.checked_at < MODEL_REFRESH_SECONDS:
            return
        self.checked_at = time.monotonic()
        settings = get_settings()
        storage = s3_client()
        try:
            metadata = json.loads(
                storage.get_object(Bucket=settings.s3_bucket, Key="ml/active/active.json")["Body"].read()
            )
        except Exception:
            return
        if self.metadata == metadata and self.session is not None:
            return
        model_path = MODEL_DIR / f"{metadata['run_id']}.onnx"
        storage.download_file(settings.s3_bucket, "ml/active/model.onnx", str(model_path))
        self.session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self.metadata = metadata


registry = ModelRegistry()


def refresh_model() -> None:
    registry.refresh()
