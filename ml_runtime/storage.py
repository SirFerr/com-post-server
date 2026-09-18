import boto3

from app.config import get_settings


def s3_client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


def read_photo(photo_key: str) -> bytes:
    settings = get_settings()
    body = s3_client().get_object(Bucket=settings.s3_bucket, Key=photo_key)["Body"]
    try:
        payload = body.read(settings.max_photo_bytes + 1)
    finally:
        body.close()
    if len(payload) > settings.max_photo_bytes:
        raise ValueError("Photo exceeds the allowed size")
    return payload
