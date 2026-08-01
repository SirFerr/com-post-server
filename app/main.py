from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .api.router import router as api_router
from .database import Base, engine, migrate_schema
from .web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    migrate_schema()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
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

    return application


app = create_app()
