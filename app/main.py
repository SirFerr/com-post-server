from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .api.router import router as api_router
from .config import get_settings
from .database import Base, engine, migrate_schema
from .rate_limit import RateLimitMiddleware
from .observability import ObservabilityMiddleware, metrics_text
from .database import SessionLocal
from sqlalchemy import text
import boto3
import urllib.request
from botocore.config import Config
from .web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
        migrate_schema()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(ObservabilityMiddleware)
    application.include_router(api_router)
    application.include_router(admin_router)
    application.include_router(web_router)
    application.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @application.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == 401 and request.url.path.startswith("/web"):
            response = RedirectResponse(f"/web/login?next={request.url.path}", status_code=303)
            response.delete_cookie("compost_session")
            response.delete_cookie("compost_csrf")
            return response
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @application.get("/", include_in_schema=False)
    def web_root():
        return RedirectResponse("/web", status_code=307)

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/health/live")
    def health_live():
        return {"status": "ok"}

    @application.get("/health/ready")
    def health_ready():
        settings = get_settings()
        checks = {}
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "unavailable"
        for name, endpoint, key, secret, bucket in (
            ("photos", settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_bucket),
            ("firmware", settings.firmware_s3_endpoint, settings.firmware_s3_access_key, settings.firmware_s3_secret_key, settings.firmware_s3_bucket),
        ):
            try:
                boto3.client(
                    "s3",
                    endpoint_url=endpoint,
                    aws_access_key_id=key,
                    aws_secret_access_key=secret,
                    config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 0}),
                ).head_bucket(Bucket=bucket)
                checks[name] = "ok"
            except Exception:
                checks[name] = "unavailable"
        try:
            with urllib.request.urlopen(f"{settings.ml_service_url.rstrip('/')}/health", timeout=2) as response:
                checks["ml"] = "ok" if response.status == 200 else "unavailable"
        except Exception:
            checks["ml"] = "unavailable"
        if any(value != "ok" for value in checks.values()):
            return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
        return {"status": "ready", "checks": checks}

    @application.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        return metrics_text()

    return application


app = create_app()
