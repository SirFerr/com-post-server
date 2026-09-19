import io

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from ..config import get_settings
from .media_client import presigned_url, upload_image


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
    except Image.DecompressionBombError:
        raise HTTPException(413, "Image resolution exceeds the allowed limit")
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(422, "Image is corrupted or unsupported")
    return payload, {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]


def store_photo(file: UploadFile) -> str:
    payload, content_type = validated_image(file)
    return upload_image(payload, content_type, file.filename or "photo.jpg")


def photo_url(key: str) -> str:
    return presigned_url(key)
