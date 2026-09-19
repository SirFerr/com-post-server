"""Photo object owner. The API and web processes use its private HTTP contract."""

import hmac
import io
import os
import re
import uuid

import boto3
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from PIL import Image, UnidentifiedImageError

from app.observability import ObservabilityMiddleware, metrics_text


app = FastAPI(title="ComPost media service")
app.add_middleware(ObservabilityMiddleware)


def _setting(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bucket() -> str:
    return _setting("S3_BUCKET", "compost-photos")


def _storage(public: bool = False):
    return boto3.client(
        "s3",
        endpoint_url=_setting("S3_PUBLIC_ENDPOINT" if public else "S3_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=_setting("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=_setting("S3_SECRET_KEY", "minioadmin"),
    )


def _authorize(x_internal_token: str | None = Header(None)) -> None:
    expected = os.environ.get("MEDIA_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(503, "Media service token is not configured")
    if not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(403, "Forbidden")


private = [Depends(_authorize)]


def _validated_image(file: UploadFile) -> tuple[bytes, str]:
    maximum = int(_setting("MAX_PHOTO_BYTES", str(12 * 1024 * 1024)))
    payload = file.file.read(maximum + 1)
    if len(payload) > maximum:
        raise HTTPException(413, "Photo exceeds the allowed size")
    if len(payload) < 128:
        raise HTTPException(422, "Image is empty or corrupted")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = image.format
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(422, "Unsupported image format")
            if image.width * image.height > int(_setting("MAX_IMAGE_PIXELS", "40000000")):
                raise HTTPException(413, "Image resolution exceeds the allowed limit")
            image.verify()
    except Image.DecompressionBombError:
        raise HTTPException(413, "Image resolution exceeds the allowed limit")
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(422, "Image is corrupted or unsupported")
    return payload, {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return metrics_text()


@app.get("/health/ready")
def ready():
    try:
        _storage().head_bucket(Bucket=_bucket())
    except Exception as exc:
        raise HTTPException(503, "Photo object store is unavailable") from exc
    return {"status": "ready"}


@app.post("/photos", dependencies=private, status_code=201)
def upload_photo(
    file: UploadFile = File(...),
    namespace: str = Query("deposits", max_length=160),
):
    if namespace != "deposits" and not re.fullmatch(r"engineering/[A-Za-z0-9_-]{1,128}", namespace):
        raise HTTPException(422, "Invalid photo namespace")
    payload, content_type = _validated_image(file)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", file.filename or "photo.jpg").strip(".-") or "photo.jpg"
    key = f"{namespace}/{uuid.uuid4()}-{safe_name[:120]}"
    try:
        _storage().put_object(Bucket=_bucket(), Key=key, Body=payload, ContentType=content_type)
    except Exception as exc:
        raise HTTPException(503, "Photo object store is unavailable") from exc
    return {"key": key}


@app.get("/objects", dependencies=private)
def list_objects(
    prefix: str = Query("", max_length=256),
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None, max_length=1024),
):
    try:
        page = _storage().list_objects_v2(
            Bucket=_bucket(), Prefix=prefix, MaxKeys=limit,
            **({"ContinuationToken": cursor} if cursor else {}),
        )
    except Exception as exc:
        raise HTTPException(503, "Photo object store is unavailable") from exc
    return {
        "objects": [
            {
                "key": item["Key"],
                "size": item["Size"],
                "modified": item["LastModified"].isoformat() if hasattr(item["LastModified"], "isoformat") else item["LastModified"],
            }
            for item in page.get("Contents", [])
        ],
        "cursor": page.get("NextContinuationToken"),
    }


@app.get("/objects/metadata", dependencies=private)
def object_metadata(key: str = Query(..., min_length=1, max_length=512)):
    try:
        head = _storage().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(404, "Storage object not found") from exc
        raise HTTPException(503, "Photo object store is unavailable") from exc
    except Exception as exc:
        raise HTTPException(503, "Photo object store is unavailable") from exc
    modified = head.get("LastModified")
    return {
        "key": key,
        "size": head.get("ContentLength", 0),
        "modified": modified.isoformat() if hasattr(modified, "isoformat") else modified,
        "content_type": head.get("ContentType", "application/octet-stream"),
        "etag": head.get("ETag", ""),
        "metadata": head.get("Metadata", {}),
    }


@app.get("/objects/url", dependencies=private)
def object_url(key: str = Query(..., min_length=1, max_length=512)):
    try:
        url = _storage(public=True).generate_presigned_url(
            "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=900,
        )
    except Exception as exc:
        raise HTTPException(503, "Photo URL is unavailable") from exc
    return {"url": url}
