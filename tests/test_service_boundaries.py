from fastapi.testclient import TestClient

from app.api_service import app as api_app
from app.web_service import app as web_app


def test_api_excludes_web_routes():
    with TestClient(api_app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/web/login").status_code == 404
        assert client.get("/auth/login").status_code != 404


def test_web_excludes_api_routes():
    with TestClient(web_app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/web/login").status_code == 200
        assert client.get("/auth/login").status_code == 404
