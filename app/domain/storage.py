import uuid

import boto3
from fastapi import HTTPException, UploadFile

from ..config import get_settings


def _image_storage(endpoint_url: str):
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


def store_photo(file: UploadFile) -> str:
    start = file.file.read(512)
    file.file.seek(0)
    if len(start) < 128:
        raise HTTPException(422, "Изображение повреждено или не содержит данных")
    is_jpeg = start.startswith(b"\xff\xd8\xff")
    is_png = start.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = start.startswith(b"RIFF") and start[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_webp):
        raise HTTPException(422, "Файл не является поддерживаемым изображением")
    settings = get_settings()
    key = f"deposits/{uuid.uuid4()}-{file.filename or 'photo.jpg'}"
    _image_storage(settings.s3_endpoint).upload_fileobj(
        file.file,
        settings.s3_bucket,
        key,
        ExtraArgs={"ContentType": file.content_type or "image/jpeg"},
    )
    return key


def photo_url(key: str) -> str:
    settings = get_settings()
    return _image_storage(settings.s3_public_endpoint).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=900,
    )
