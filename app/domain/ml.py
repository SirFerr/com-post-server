import httpx
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from ..config import get_settings


class MLReview(BaseModel):
    status: Literal["LIKELY_VALID", "LIKELY_INVALID", "NEEDS_MANUAL_REVIEW"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    violations: list[str] = Field(default_factory=list, max_length=100)


def request_ml_review(photo_key: str) -> dict:
    try:
        response = httpx.post(
            f"{get_settings().ml_service_url}/analyze",
            json={"photo_key": photo_key},
            timeout=5,
        )
        response.raise_for_status()
        return MLReview.model_validate(response.json()).model_dump()
    except (httpx.HTTPError, ValueError, ValidationError):
        return {"status": "NEEDS_MANUAL_REVIEW", "confidence": 0.0, "violations": []}
