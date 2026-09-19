from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "sqlite:///./compost.db"
    jwt_secret: str = "local-development-secret-change-me"
    jwt_ttl_minutes: int = 1_440
    refresh_ttl_days: int = 30
    web_session_ttl_days: int = 30
    command_ttl_seconds: int = 60
    s3_endpoint: str = "http://minio:9000"
    s3_public_endpoint: str = "http://192.168.1.212:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "compost-photos"
    firmware_s3_endpoint: str = "http://firmware-storage:9100"
    firmware_s3_access_key: str = "firmwareadmin"
    firmware_s3_secret_key: str = "firmware-local-secret"
    firmware_s3_bucket: str = "compost-firmware"
    ml_service_url: str = "http://ml-service:8001"
    media_service_url: str = "http://media-service:8003"
    media_internal_token: str = "local-media-token-change-me"
    telemetry_service_url: str = "http://telemetry-service:8004"
    telemetry_internal_token: str = "local-telemetry-token-change-me"
    max_photo_bytes: int = 12 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    cookie_secure: bool = False
    auto_create_schema: bool = True
    trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128"

    def validate_runtime(self) -> None:
        if self.environment.lower() != "production":
            return
        weak = {
            "local-development-secret-change-me",
            "minioadmin",
            "firmware-local-secret",
        }
        if len(self.jwt_secret) < 32 or self.jwt_secret in weak:
            raise RuntimeError("JWT_SECRET must be a unique secret of at least 32 characters in production")
        if self.s3_access_key in weak or self.s3_secret_key in weak:
            raise RuntimeError("Default S3 credentials are forbidden in production")
        if self.firmware_s3_access_key in weak or self.firmware_s3_secret_key in weak:
            raise RuntimeError("Default firmware storage credentials are forbidden in production")
        if not self.cookie_secure:
            raise RuntimeError("COOKIE_SECURE must be enabled in production")
        if len(self.media_internal_token) < 32 or self.media_internal_token == "local-media-token-change-me":
            raise RuntimeError("MEDIA_INTERNAL_TOKEN must be a unique secret of at least 32 characters in production")
        if len(self.telemetry_internal_token) < 32 or self.telemetry_internal_token == "local-telemetry-token-change-me":
            raise RuntimeError("TELEMETRY_INTERNAL_TOKEN must be a unique secret of at least 32 characters in production")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
