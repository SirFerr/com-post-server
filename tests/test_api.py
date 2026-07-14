import jwt

from app.config import get_settings
from app.database import SessionLocal
from app.models import AccessSession, DeviceCommand, Review, SessionStatus, User
from tests.conftest import token


def auth(value):
    return {"Authorization": f"Bearer {value}"}


def test_health_and_login(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/", follow_redirects=False).headers["location"] == "/web"
    assert client.post("/auth/login", json={"email": "user@example.com", "password": "bad-password"}).status_code == 401
    assert client.post("/auth/login", json={"email": "USER@EXAMPLE.COM", "password": "Password1!"}).status_code == 200
    assert token(client)


def test_expired_and_invalid_tokens_have_distinct_errors(client):
    expired = jwt.encode({"sub": "user-1", "exp": 1}, get_settings().jwt_secret, algorithm="HS256")
    expired_response = client.get("/profile", headers=auth(expired))
    invalid_response = client.get("/profile", headers=auth("not-a-token"))
    assert expired_response.status_code == 401
    assert expired_response.json()["detail"] == "Access token expired"
    assert invalid_response.status_code == 401
    assert invalid_response.json()["detail"] == "Invalid access token"


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


def test_registration_validates_email_and_password_strength(client):
    assert client.post("/auth/register", json={"email": "not-an-email", "password": "Password2!", "full_name": "Тест"}).status_code == 422
    assert client.post("/auth/register", json={"email": "weak@example.com", "password": "password", "full_name": "Тест"}).status_code == 422
    assert client.post("/auth/register", json={"email": "plain@example.com", "password": "Password2", "full_name": "Без спецсимвола"}).status_code == 201


def test_account_password_change_and_delete_require_current_password(client):
    headers = auth(token(client))
    assert client.post("/profile/change-password", json={"current_password": "wrong-pass", "new_password": "Changed123"}, headers=headers).status_code == 403
    assert client.post("/profile/change-password", json={"current_password": "Password1!", "new_password": "Changed123"}, headers=headers).status_code == 200
    changed_headers = auth(client.post("/auth/login", json={"email": "user@example.com", "password": "Changed123"}).json()["access_token"])
    assert client.post("/profile/delete", json={"current_password": "wrong-pass"}, headers=changed_headers).status_code == 403
    assert client.post("/profile/delete", json={"current_password": "Changed123"}, headers=changed_headers).status_code == 200


def test_full_report_and_confirmed_composter_delete(client):
    user_headers = auth(token(client))
    admin_headers = auth(token(client, "admin@example.com"))
    assert client.post("/composters/composter-1/report-full", headers=user_headers).status_code == 200
    equipment = client.get("/admin/composters", headers=admin_headers).json()[0]
    assert equipment["needs_emptying"] is True
    assert client.post("/admin/composters/composter-1/delete", json={"current_password": "wrong-pass"}, headers=admin_headers).status_code == 403
    assert client.post("/admin/composters/composter-1/delete", json={"current_password": "Password1!"}, headers=admin_headers).status_code == 200


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


def test_active_session_can_retry_close(client):
    with SessionLocal() as db:
        session = AccessSession(id="retry-close-session", user_id="user-1", composter_id="composter-1", status=SessionStatus.CLOSE_REQUESTED)
        db.add(session)
        db.commit()
    headers = auth(token(client))
    active = client.get("/sessions/active", headers=headers)
    assert active.status_code == 200
    assert active.json()["session_id"] == "retry-close-session"
    assert active.json()["close_pending"] is True
    retry = client.post("/sessions/retry-close-session/retry-close", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["command"]["action"] == "CLOSE"


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
    updated = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "is_blocked": True, "ban_reason": "manual test"}, headers=headers)
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


def test_password_allows_but_does_not_require_special_characters(client):
    plain = client.post("/auth/register", json={"email": "plain@example.com", "password": "Strong123", "full_name": "Plain"})
    special = client.post("/auth/register", json={"email": "special@example.com", "password": "Strong123!", "full_name": "Special"})
    assert plain.status_code == 201
    assert special.status_code == 201


def test_admin_can_onboard_composter_with_existing_firmware_secret(client):
    headers = auth(token(client, "admin@example.com"))
    secret = "a" * 64
    response = client.post("/admin/composters", json={"name": "BLE device", "device_id": "composter_000042", "latitude": 55.75, "longitude": 37.61, "secret": secret}, headers=headers)
    assert response.status_code == 200
    assert response.json()["device_secret"] == secret
    assert response.json()["qr_payload"].startswith("compost://composter/")


def test_composter_onboarding_resume_is_idempotent(client):
    headers = auth(token(client, "engineer@example.com"))
    payload = {"name": "BLE device", "device_id": "composter_resume", "latitude": 55.75, "longitude": 37.61, "is_available": False}
    first = client.post("/admin/composters", json=payload, headers=headers)
    activated = client.post(f"/admin/composters/{first.json()['id']}/activate-provisioned", headers=headers)
    second = client.post("/admin/composters", json={**payload, "name": "Updated point"}, headers=headers)
    assert first.status_code == 200
    assert activated.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["device_secret"] == first.json()["device_secret"]
    equipment = client.get("/admin/composters", headers=headers).json()
    assert next(row for row in equipment if row["id"] == first.json()["id"])["is_available"] is True


def test_android_user_history_records_actor_role_and_block_changes(client):
    headers = auth(token(client, "admin@example.com"))
    role_change = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "role": "MODERATOR"}, headers=headers)
    block = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "is_blocked": True, "ban_reason": "Test"}, headers=headers)
    assert role_change.status_code == 200
    assert block.status_code == 200
    history = client.get("/admin/users/user-1/history", headers=headers)
    assert history.status_code == 200
    assert {row["action"] for row in history.json()} >= {"USER_ROLE_CHANGED", "USER_BLOCKED"}
    assert all(row["actor_email"] == "admin@example.com" for row in history.json())


def test_android_equipment_history_contains_access_and_full_report(client):
    user_headers = auth(token(client))
    admin_headers = auth(token(client, "admin@example.com"))
    assert client.post("/composters/composter-1/report-full", headers=user_headers).status_code == 200
    assert client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=user_headers).status_code == 200
    history = client.get("/admin/composters/composter-1/history", headers=admin_headers)
    assert history.status_code == 200
    assert {row["action"] for row in history.json()} >= {"FULL_REPORTED", "OPEN_REQUESTED"}
