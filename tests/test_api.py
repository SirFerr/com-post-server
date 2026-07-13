from app.database import SessionLocal
from app.models import AccessSession, Review, User
from tests.conftest import token


def auth(value):
    return {"Authorization": f"Bearer {value}"}


def test_health_and_login(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/auth/login", json={"email": "user@example.com", "password": "bad-password"}).status_code == 401
    assert token(client)


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
