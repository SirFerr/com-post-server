from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./compost.db"
    jwt_secret: str = "local-development-secret-change-me"
    jwt_ttl_minutes: int = 60
    command_ttl_seconds: int = 60
    s3_endpoint: str = "http://minio:9000"
    s3_public_endpoint: str = "http://192.168.1.212:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "compost-photos"
    ml_service_url: str = "http://ml-service:8001"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
