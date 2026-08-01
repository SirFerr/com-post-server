import httpx

from ..config import get_settings


def request_ml_review(photo_key: str) -> dict:
    try:
        response = httpx.post(
            f"{get_settings().ml_service_url}/analyze",
            json={"photo_key": photo_key},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return {"status": "NEEDS_MANUAL_REVIEW", "confidence": 0.0, "violations": []}
