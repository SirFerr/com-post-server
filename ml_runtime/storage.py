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
    return s3_client().get_object(Bucket=settings.s3_bucket, Key=photo_key)["Body"].read()
