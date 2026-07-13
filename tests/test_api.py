from app.database import SessionLocal
from app.models import AccessSession, Review, User
from tests.conftest import token


def auth(value):
    return {"Authorization": f"Bearer {value}"}


def test_health_and_login(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/auth/login", json={"email": "user@example.com", "password": "bad-password"}).status_code == 401
    assert token(client)


def test_register_profile_and_empty_history(client):
    registered = client.post("/auth/register", json={"email": "New@Example.com", "password": "Password2!", "full_name": "Новый пользователь"})
    assert registered.status_code == 201
    headers = auth(registered.json()["access_token"])
    profile = client.get("/profile", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["email"] == "new@example.com"
    assert profile.json()["active_violations"] == 0
    assert client.get("/profile/deposits", headers=headers).json() == []
    assert client.post("/auth/register", json={"email": "new@example.com", "password": "Password2!", "full_name": "Дубль"}).status_code == 409


def test_access_rejects_bad_location_and_replay(client):
    headers = auth(token(client))
    far = client.post("/composters/composter-1/access", json={"latitude": 1, "longitude": 1}, headers=headers)
    assert far.status_code == 403
    granted = client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=headers)
    assert granted.status_code == 200
    body = granted.json()
    ack = {"command_id": body["command"]["commandId"], "status": "SUCCESS"}
    assert client.post(f"/sessions/{body['session_id']}/ack", json=ack, headers=headers).status_code == 200
    assert client.post(f"/sessions/{body['session_id']}/ack", json=ack, headers=headers).status_code == 409


def test_third_violation_blocks_user(client):
    moderator = auth(token(client, "moderator@example.com"))
    with SessionLocal() as db:
        for index in range(3):
            session = AccessSession(id=f"session-{index}", user_id="user-1", composter_id="composter-1")
            db.add(session)
            db.flush()
            db.add(Review(id=f"review-{index}", session_id=session.id, photo_key=f"photo-{index}"))
        db.commit()
    for index in range(3):
        response = client.post(f"/moderation/reviews/review-{index}", json={"approved": False, "violation_reason": "PLASTIC"}, headers=moderator)
        assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(User, "user-1").is_blocked is True


def test_moderation_queue_contains_user_composter_and_time(client):
    with SessionLocal() as db:
        session = AccessSession(id="session-details", user_id="user-1", composter_id="composter-1")
        db.add(session)
        db.flush()
        db.add(Review(id="review-details", session_id=session.id, photo_key="photo-details"))
        db.commit()
    rows = client.get("/moderation/reviews", headers=auth(token(client, "moderator@example.com"))).json()
    row = next(item for item in rows if item["id"] == "review-details")
    assert row["user_email"] == "user@example.com"
    assert row["composter_name"] == "Test"
    assert row["created_at"]


def test_user_cannot_open_moderation_queue(client):
    assert client.get("/moderation/reviews", headers=auth(token(client))).status_code == 403


def test_admin_dashboard_and_user_blocking(client):
    headers = auth(token(client, "admin@example.com"))
    dashboard = client.get("/admin/dashboard", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["composters"] == 1
    updated = client.patch("/admin/users/user-1", json={"is_blocked": True, "ban_reason": "manual test"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["is_blocked"] is True
    with SessionLocal() as db:
        assert db.get(User, "user-1").ban_reason == "manual test"
