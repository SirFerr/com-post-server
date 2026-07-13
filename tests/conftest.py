import os

os.environ["DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Composter, Role, User
from app.security import hash_password

@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add_all([
            User(id="user-1", email="user@example.com", password_hash=hash_password("Password1!"), role=Role.USER),
            User(id="mod-1", email="moderator@example.com", password_hash=hash_password("Password1!"), role=Role.MODERATOR),
            User(id="admin-1", email="admin@example.com", password_hash=hash_password("Password1!"), role=Role.ADMIN),
            Composter(id="composter-1", name="Test", device_id="device-1", latitude=55.75, longitude=37.61, radius_m=200, secret="test-secret"),
        ])
        db.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def token(client, email="user@example.com"):
    response = client.post("/auth/login", json={"email": email, "password": "Password1!"})
    return response.json()["access_token"]
