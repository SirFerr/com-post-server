import io
import re
import uuid

import boto3
from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from ..config import get_settings


def _image_storage(endpoint_url: str):
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


def validated_image(file: UploadFile) -> tuple[bytes, str]:
    settings = get_settings()
    payload = file.file.read(settings.max_photo_bytes + 1)
    if len(payload) > settings.max_photo_bytes:
        raise HTTPException(413, "Photo exceeds the allowed size")
    if len(payload) < 128:
        raise HTTPException(422, "Image is empty or corrupted")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = image.format
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(422, "Unsupported image format")
            if image.width * image.height > settings.max_image_pixels:
                raise HTTPException(413, "Image resolution exceeds the allowed limit")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(422, "Image is corrupted or unsupported")
    return payload, {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]


def store_photo(file: UploadFile) -> str:
    payload, content_type = validated_image(file)
    settings = get_settings()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", file.filename or "photo.jpg").strip(".-") or "photo.jpg"
    key = f"deposits/{uuid.uuid4()}-{safe_name[:120]}"
    _image_storage(settings.s3_endpoint).upload_fileobj(
        io.BytesIO(payload),
        settings.s3_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def photo_url(key: str) -> str:
    settings = get_settings()
    return _image_storage(settings.s3_public_endpoint).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=900,
    )
