from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .api.router import router as api_router
from .api.health import router as health_router
from .config import get_settings
from .database import Base, engine, migrate_schema
from .rate_limit import RateLimitMiddleware
from .observability import ObservabilityMiddleware
from .web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
        migrate_schema()
    yield


def create_app(*, role: str = "combined") -> FastAPI:
    if role not in {"combined", "api", "web"}:
        raise ValueError(f"Unknown application role: {role}")
    application = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(ObservabilityMiddleware)
    application.include_router(health_router)
    if role in {"combined", "api"}:
        application.include_router(api_router)
        application.include_router(admin_router)
    if role in {"combined", "web"}:
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

    if role in {"combined", "web"}:
        @application.get("/", include_in_schema=False)
        def web_root():
            return RedirectResponse("/web", status_code=307)

    return application


app = create_app()
