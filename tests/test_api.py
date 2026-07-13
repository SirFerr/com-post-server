from app.database import SessionLocal
from app.models import AccessSession, DeviceCommand, Review, SessionStatus, User
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


def test_staff_debug_command_is_signed_and_can_be_acknowledged(client):
    headers = auth(token(client, "engineer@example.com"))
    response = client.post("/admin/composters/composter-1/debug-command", json={"action": "OPEN"}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["command"]["action"] == "OPEN"
    assert body["command"]["signature"]
    ack = client.post(f"/sessions/{body['session_id']}/ack", json={"command_id": body["command"]["commandId"], "status": "SUCCESS"}, headers=headers)
    assert ack.status_code == 200
    with SessionLocal() as db:
        assert db.get(AccessSession, body["session_id"]).status == SessionStatus.CLOSED


def test_unfinished_staff_diagnostic_does_not_block_regular_user(client):
    engineer = auth(token(client, "engineer@example.com"))
    diagnostic = client.post("/admin/composters/composter-1/debug-command", json={"action": "OPEN"}, headers=engineer)
    assert diagnostic.status_code == 200

    user = auth(token(client))
    granted = client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=user)
    assert granted.status_code == 200
    body = granted.json()
    assert client.post(
        f"/sessions/{body['session_id']}/ack",
        json={"command_id": body["command"]["commandId"], "status": "CONNECTION_FAILED"},
        headers=user,
    ).status_code == 200


def test_expired_unacknowledged_user_command_is_cleaned_before_access(client):
    with SessionLocal() as db:
        stale = AccessSession(id="stale-session", user_id="user-1", composter_id="composter-1")
        db.add(stale)
        db.flush()
        db.add(DeviceCommand(
            id="stale-command",
            session_id=stale.id,
            action="OPEN",
            nonce="stale-nonce",
            issued_at=1,
            expires_at=2,
            acknowledged=False,
        ))
        db.commit()

    user = auth(token(client))
    granted = client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=user)
    assert granted.status_code == 200
    with SessionLocal() as db:
        assert db.get(AccessSession, "stale-session").status == SessionStatus.FAILED


def test_staff_web_login_and_role_sections(client):
    login = client.post("/web/login", data={"email": "admin@example.com", "password": "Password1!"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get("/web/dashboard")
    assert page.status_code == 200
    assert "Пользователи и сотрудники" in page.text
    assert "Оборудование" in page.text


def test_regular_user_cannot_open_staff_web(client):
    login = client.post("/web/login", data={"email": "user@example.com", "password": "Password1!"})
    assert login.status_code == 401


def test_admin_can_onboard_composter_with_existing_firmware_secret(client):
    headers = auth(token(client, "admin@example.com"))
    secret = "a" * 64
    response = client.post("/admin/composters", json={"name": "BLE device", "device_id": "composter_000042", "latitude": 55.75, "longitude": 37.61, "secret": secret}, headers=headers)
    assert response.status_code == 200
    assert response.json()["device_secret"] == secret
    assert response.json()["qr_payload"].startswith("compost://composter/")
