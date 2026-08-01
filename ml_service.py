"""ASGI entrypoint kept stable for ``uvicorn ml_service:app``."""

from ml_runtime.api import app
from ml_runtime.inference import heuristic_prediction, trained_prediction
from ml_runtime.preprocessing import prepare
from ml_runtime.registry import refresh_model

__all__ = [
    "app",
    "heuristic_prediction",
    "prepare",
    "refresh_model",
    "trained_prediction",
]
