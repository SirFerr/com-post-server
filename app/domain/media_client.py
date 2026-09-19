"""Private HTTP client for the photo service; no S3 access in API or web."""

from datetime import datetime

import httpx
from fastapi import HTTPException

from ..config import get_settings


def _request(method: str, path: str, **kwargs) -> dict:
    settings = get_settings()
    token = settings.media_internal_token
    if not token:
        raise HTTPException(503, "Media service token is not configured")
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.request(
                method,
                f"{settings.media_service_url.rstrip('/')}{path}",
                headers={"X-Internal-Token": token},
                **kwargs,
            )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {404, 413, 415, 422}:
            try:
                detail = exc.response.json().get("detail", "Media request failed")
            except ValueError:
                detail = "Media request failed"
            raise HTTPException(status, detail) from exc
        raise HTTPException(503, "Media service is unavailable") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(503, "Media service is unavailable") from exc


def upload_image(payload: bytes, content_type: str, filename: str, namespace: str = "deposits") -> str:
    result = _request(
        "POST", "/photos", params={"namespace": namespace},
        files={"file": (filename, payload, content_type)},
    )
    return result["key"]


def presigned_url(key: str) -> str:
    return _request("GET", "/objects/url", params={"key": key})["url"]


def list_media_objects(prefix: str = "", limit: int = 100, cursor: str | None = None) -> dict:
    params = {"prefix": prefix, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _request("GET", "/objects", params=params)


def media_object(key: str) -> dict:
    return _request("GET", "/objects/metadata", params={"key": key})


def _modified_at(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime) or value is None:
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MediaStorageClient:
    """Small compatibility adapter for existing admin storage presenters."""

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", MaxKeys: int = 100, ContinuationToken: str | None = None):
        result = list_media_objects(Prefix, MaxKeys, ContinuationToken)
        return {
            "Contents": [
                {"Key": row["key"], "Size": row["size"], "LastModified": _modified_at(row["modified"])}
                for row in result["objects"]
            ],
            "NextContinuationToken": result.get("cursor"),
        }

    def head_object(self, *, Bucket: str, Key: str):
        row = media_object(Key)
        return {
            "ContentLength": row["size"],
            "LastModified": _modified_at(row.get("modified")),
            "ContentType": row["content_type"],
            "ETag": row.get("etag", ""),
            "Metadata": row.get("metadata", {}),
        }


def storage_client() -> MediaStorageClient:
    return MediaStorageClient()
