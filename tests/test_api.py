import io
import json
import jwt

from app import web
from app.api import storage as storage_api
from app.config import get_settings
from app.database import SessionLocal
from app.models import AccessSession, AuditLog, DeviceCommand, Incident, Review, SessionStatus, StorageObjectAnnotation, User
from tests.conftest import token


def auth(value):
    return {"Authorization": f"Bearer {value}"}


def test_audit_entity_id_accepts_storage_keys():
    assert AuditLog.__table__.c.entity_id.type.length == 512


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


def test_expired_web_session_redirects_to_login(client):
    expired = jwt.encode({"sub": "admin-1", "exp": 1}, get_settings().jwt_secret, algorithm="HS256")
    response = client.get("/web/dashboard", cookies={"compost_session": expired}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/login")


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


def test_staff_can_confirm_full_state_without_password(client):
    moderator_headers = auth(token(client, "moderator@example.com"))
    engineer_headers = auth(token(client, "engineer@example.com"))
    user_headers = auth(token(client))
    endpoint = "/admin/composters/composter-1/full-state"

    assert client.post(endpoint, json={"needs_emptying": True}, headers=moderator_headers).status_code == 200
    assert client.get("/admin/composters", headers=moderator_headers).json()[0]["needs_emptying"] is True
    assert client.post(endpoint, json={"needs_emptying": False}, headers=engineer_headers).status_code == 200
    assert client.get("/admin/composters", headers=engineer_headers).json()[0]["needs_emptying"] is False
    assert client.post(endpoint, json={"needs_emptying": True}, headers=user_headers).status_code == 403


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
        assert db.get(Review, "review-0").comment == "Обнаружено нарушение"
    history = client.get("/admin/users/user-1/history", headers=moderator).json()
    photo_events = [row for row in history if row["photo_url"]]
    assert len(photo_events) == 3
    assert all(row["composter_id"] == "composter-1" for row in photo_events)


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
    assert row["composter_id"] == "composter-1"
    assert row["composter_name"] == "Test"
    assert row["created_at"]


def test_moderation_history_returns_annotations_and_reviewer(client):
    moderator = auth(token(client, "moderator@example.com"))
    with SessionLocal() as db:
        session = AccessSession(id="session-history", user_id="user-1", composter_id="composter-1")
        db.add(session)
        db.flush()
        db.add(Review(id="review-history", session_id=session.id, photo_key="photo-history"))
        db.commit()
    boxes = [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4, "label": "plastic"}]
    result = client.post("/moderation/reviews/review-history", json={"approved": False, "violation_reason": "PLASTIC", "annotations": boxes}, headers=moderator)
    assert result.status_code == 200
    history = client.get("/moderation/history", headers=moderator)
    assert history.status_code == 200
    row = next(item for item in history.json() if item["id"] == "review-history")
    assert row["annotations"] == boxes
    assert row["reviewer_name"] == "moderator@example.com"


def test_user_cannot_open_moderation_queue(client):
    assert client.get("/moderation/reviews", headers=auth(token(client))).status_code == 403


def test_blocked_user_can_manage_account_but_cannot_use_composters(client):
    headers = auth(token(client))
    with SessionLocal() as db:
        user = db.get(User, "user-1")
        user.is_blocked = True
        user.ban_reason = "Тестовая блокировка"
        db.commit()
    profile = client.get("/profile", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["is_blocked"] is True
    assert profile.json()["ban_reason"] == "Тестовая блокировка"
    assert client.get("/profile/sessions", headers=headers).status_code == 200
    assert client.get("/profile/deposits", headers=headers).status_code == 200
    assert client.get("/composters/resolve-qr/composter-1", headers=headers).status_code == 403
    assert client.post("/composters/composter-1/report-full", headers=headers).status_code == 403
    assert client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=headers).status_code == 403


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
    assert "/static/admin.css?v=23" in page.text
    assert "Состояние системы" in page.text
    assert "Оборудование" in page.text
    assert "Последние действия" not in page.text

    equipment = client.get("/web/equipment")
    assert equipment.status_code == 200
    assert "/web/equipment/composter-1" in equipment.text
    assert "current_password" not in equipment.text
    equipment_detail = client.get("/web/equipment/composter-1")
    assert equipment_detail.status_code == 200
    assert "equipment-confirm" in equipment_detail.text
    assert 'data-history-filter="ACCESS"' in equipment_detail.text
    assert 'data-history-filter="FULL"' in equipment_detail.text
    assert 'data-history-filter="SETTINGS"' in equipment_detail.text
    assert "Действия и фотографии оборудования" in equipment_detail.text

    users = client.get("/web/users")
    assert users.status_code == 200
    assert "/web/users/user-1" in users.text
    assert "current_password" not in users.text
    user_detail = client.get("/web/users/user-1")
    assert user_detail.status_code == 200
    assert "user-confirm" in user_detail.text
    assert 'data-history-filter="POINTS"' in user_detail.text
    assert 'data-history-filter="BANS"' in user_detail.text
    assert 'data-history-filter="ROLES"' in user_detail.text
    assert "Действия, проверки и фотографии" in user_detail.text
    assert "Сессии компостирования" not in user_detail.text
    role_change = client.post(
        "/web/users/user-1",
        data={"role": "MODERATOR", "current_password": "Password1!", "csrf_token": client.cookies.get("compost_csrf")},
        follow_redirects=False,
    )
    assert role_change.status_code == 303
    with SessionLocal() as db:
        assert db.get(User, "user-1").role.value == "MODERATOR"
    with SessionLocal() as db:
        db.add(AuditLog(entity="composter", entity_id="composter-1", action="COMPOSTER_UPDATED", user_id="admin-1"))
        db.commit()
    admin_detail = client.get("/web/users/admin-1")
    assert admin_detail.status_code == 200
    assert "Настройки компостера изменены" in admin_detail.text
    assert "Сессии компостирования" not in admin_detail.text

    with SessionLocal() as db:
        session = AccessSession(id="web-review-session", user_id="user-1", composter_id="composter-1")
        db.add(session)
        db.flush()
        db.add(Review(id="web-review", session_id=session.id, photo_key="web-review-photo"))
        db.commit()
    moderation = client.get("/web/moderation")
    assert moderation.status_code == 200
    assert "NEEDS_MANUAL_REVIEW" not in moderation.text
    assert "/full-state" in moderation.text
    map_page = client.get("/web/map")
    assert map_page.status_code == 200
    assert "composter-map" in map_page.text
    assert "/static/vendor/leaflet.css?v=1" in map_page.text
    assert "/static/vendor/leaflet.js?v=1" in map_page.text
    assert client.get("/static/vendor/leaflet.js").status_code == 200
    flasher = client.get("/web/flasher")
    assert flasher.status_code == 200
    assert "зажмите кнопку при подключении ESP" in flasher.text
    assert "/static/vendor/esptool-js.js" in flasher.text
    assert client.get("/static/vendor/esptool-js.js").status_code == 200


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


def test_android_user_api_blocks_role_changes_and_records_block_changes(client):
    headers = auth(token(client, "admin@example.com"))
    role_change = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "role": "MODERATOR"}, headers=headers)
    block = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "is_blocked": True, "ban_reason": "Test"}, headers=headers)
    assert role_change.status_code == 403
    assert role_change.json()["detail"] == "Roles can only be changed from the web console"
    assert block.status_code == 200
    history = client.get("/admin/users/user-1/history", headers=headers)
    assert history.status_code == 200
    assert {row["action"] for row in history.json()} >= {"USER_BLOCKED"}
    assert "USER_ROLE_CHANGED" not in {row["action"] for row in history.json()}
    assert all(row["actor_email"] == "admin@example.com" for row in history.json())


def test_engineer_cannot_change_user(client):
    headers = auth(token(client, "engineer@example.com"))
    response = client.patch("/admin/users/user-1", json={"current_password": "Password1!", "is_blocked": True}, headers=headers)
    assert response.status_code == 403


def test_mobile_storage_list_and_detail(client, monkeypatch):
    class FakeStorage:
        def list_objects_v2(self, **_):
            return {"Contents": [{"Key": "test/deposits/photo.jpg", "Size": 2048, "LastModified": "2026-08-01T10:00:00Z"}]}

        def head_object(self, **_):
            return {"ContentLength": 2048, "LastModified": "2026-08-01T10:00:00Z", "ContentType": "image/jpeg", "ETag": '"abc"'}

    monkeypatch.setattr(storage_api, "storage_client", lambda: FakeStorage())
    headers = auth(token(client, "engineer@example.com"))
    listing = client.get("/admin/storage", headers=headers)
    assert listing.status_code == 200
    assert listing.json()[0]["key"] == "test/deposits/photo.jpg"
    detail = client.get("/admin/storage/object/test/deposits/photo.jpg", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["content_type"] == "image/jpeg"
    assert client.get("/admin/storage", headers=auth(token(client))).status_code == 403


def test_android_equipment_history_contains_access_and_full_report(client):
    user_headers = auth(token(client))
    admin_headers = auth(token(client, "admin@example.com"))
    assert client.post("/composters/composter-1/report-full", headers=user_headers).status_code == 200
    assert client.post("/composters/composter-1/access", json={"latitude": 55.75, "longitude": 37.61}, headers=user_headers).status_code == 200
    history = client.get("/admin/composters/composter-1/history", headers=admin_headers)
    assert history.status_code == 200
    assert {row["action"] for row in history.json()} >= {"FULL_REPORTED", "OPEN_REQUESTED"}


def test_refresh_tokens_rotate_and_sessions_can_be_revoked(client):
    login = client.post("/auth/login", json={"email": "user@example.com", "password": "Password1!", "device_name": "Pixel test"})
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]
    rotated = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != refresh_token
    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401
    headers = auth(rotated.json()["access_token"])
    sessions = client.get("/profile/sessions", headers=headers).json()
    assert any(row["device_name"] == "Pixel test" for row in sessions)
    assert client.delete(f"/profile/sessions/{sessions[0]['id']}", headers=headers).status_code == 200


def test_incidents_maintenance_score_and_dataset_workflows(client, monkeypatch):
    admin = auth(token(client, "admin@example.com"))
    engineer = auth(token(client, "engineer@example.com"))
    incident = client.post("/admin/incidents", json={"composter_id": "composter-1", "kind": "LOCK", "severity": "HIGH", "title": "Замок не отвечает"}, headers=engineer)
    assert incident.status_code == 200
    incident_id = incident.json()["id"]
    assert client.patch(f"/admin/incidents/{incident_id}", json={"status": "RESOLVED"}, headers=engineer).json()["status"] == "RESOLVED"
    contamination = client.post(
        "/admin/composters/composter-1/contamination-report",
        data={"comment": "Пластик в контейнере"},
        files={"file": ("contamination.jpg", io.BytesIO(b"\xff\xd8\xff\xd9"), "image/jpeg")},
        headers=engineer,
    )
    assert contamination.status_code == 200
    assert contamination.json()["photo_url"]
    equipment_history = client.get("/admin/composters/composter-1/history", headers=engineer).json()
    contamination_event = next(row for row in equipment_history if row["action"] == "CONTAMINATION_REPORTED")
    assert contamination_event["photo_url"]
    active = client.get("/admin/violations", headers=engineer).json()
    reported = next(row for row in active if row["id"] == contamination.json()["id"])
    assert reported["kind"] == "CONTAMINATION"
    assert reported["source_name"] == "engineer@example.com"
    assert reported["photo_url"]
    manual = client.post(
        "/admin/incidents/report",
        data={"composter_id": "composter-1", "kind": "DEVICE", "severity": "HIGH", "title": "Ручной инцидент", "description": "Создан из формы"},
        files={"file": ("manual.jpg", io.BytesIO(b"\xff\xd8\xff\xd9"), "image/jpeg")},
        headers=engineer,
    )
    assert manual.status_code == 200
    assert manual.json()["photo_url"]
    assert client.post(f"/admin/violations/{reported['id']}/resolve", headers=engineer).status_code == 200
    history = client.get("/admin/violations?active_only=false", headers=engineer).json()
    resolved = next(row for row in history if row["id"] == reported["id"])
    assert resolved["is_active"] is False
    assert resolved["resolved_by"] == "engineer-1"
    assert client.post("/admin/composters/composter-1/maintenance-mode", json={"enabled": True}, headers=engineer).status_code == 200
    assert client.post("/admin/composters/composter-1/maintenance", json={"action": "LOCK_REPLACED", "notes": "Проверено"}, headers=engineer).status_code == 200
    adjusted = client.post("/admin/users/user-1/score", json={"current_password": "Password1!", "amount": 25, "reason": "Компенсация"}, headers=admin)
    assert adjusted.json()["points"] == 25
    assert client.get("/admin/users/user-1/score-transactions", headers=admin).json()[0]["amount"] == 25
    dataset = client.post("/admin/ml/datasets", headers=admin)
    assert dataset.status_code == 200
    assert dataset.json()["version"] == 1
    manifest = client.get("/admin/ml/datasets/1", headers=admin)
    assert manifest.status_code == 200
    assert manifest.json()["schema_version"] == 2
    assert manifest.json()["split_strategy"] == "grouped-by-composter-sha256-v1"
    assert client.post("/web/login", data={"email": "admin@example.com", "password": "Password1!"}, follow_redirects=False).status_code == 303
    violation_detail = client.get(f"/web/violations/{manual.json()['id']}")
    assert violation_detail.status_code == 200
    assert "Ручной инцидент" in violation_detail.text
    with SessionLocal() as db:
        manual_row = db.get(Incident, manual.json()["id"])
        assert manual_row and manual_row.photo_key
        object_key = manual_row.photo_key
        session = AccessSession(id="detail-session", user_id="user-1", composter_id="composter-1")
        db.add(session)
        db.flush()
        db.add(Review(id="detail-review", session_id=session.id, photo_key=object_key))
        db.commit()
    class FakeS3:
        def head_object(self, **_):
            return {"ContentLength": 4, "ContentType": "image/jpeg", "ETag": '"test-etag"', "Metadata": {}}

        def generate_presigned_url(self, *_args, **_kwargs):
            return "http://example.test/photo.jpg"

    monkeypatch.setattr(web.boto3, "client", lambda *_args, **_kwargs: FakeS3())
    object_detail = client.get(f"/web/storage/object/{object_key}")
    assert object_detail.status_code == 200
    assert object_key in object_detail.text
    assert 'id="storage-annotation-dialog"' in object_detail.text
    assert 'data-box-action="move"' in object_detail.text
    assert "Убрать всю разметку с изображения?" in object_detail.text
    csrf_token = client.cookies.get("compost_csrf")
    boxes = [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4, "label": "contamination"}]
    saved_annotations = client.post(
        f"/web/storage/annotations/{object_key}",
        data={"csrf_token": csrf_token, "annotations": json.dumps(boxes)},
        follow_redirects=False,
    )
    assert saved_annotations.status_code == 303
    with SessionLocal() as db:
        annotation = db.get(StorageObjectAnnotation, object_key)
        linked_review = db.get(Review, "detail-review")
        assert annotation and json.loads(annotation.annotations) == boxes
        assert linked_review and json.loads(linked_review.annotations) == boxes
    deposit_detail = client.get("/web/deposits/detail-session")
    assert deposit_detail.status_code == 200
    assert "detail-session" in deposit_detail.text
    history_detail = client.get(f"/web/history/{contamination_event['id']}")
    assert history_detail.status_code == 200
    assert "CONTAMINATION_REPORTED" in history_detail.text
    web_created = client.post(
        "/web/violations/create",
        data={"csrf_token": csrf_token, "composter_id": "composter-1", "kind": "OTHER", "severity": "LOW", "title": "Создано в вебе", "description": "Проверка формы"},
        follow_redirects=False,
    )
    assert web_created.status_code == 303
    assert web_created.headers["location"].startswith("/web/violations/")
    legacy_incidents = client.get("/web/incidents", follow_redirects=False)
    assert legacy_incidents.status_code == 303
    assert legacy_incidents.headers["location"].startswith("/web/violations")
