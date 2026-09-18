import io
import math
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import event
from starlette.requests import Request

from app import observability
from app.database import SessionLocal, engine
from app.domain import ml, storage
from app.domain.geo import distance_m
from app.models import AccessSession, Review
from app.queries.deposits import user_deposits
from app.rate_limit import RateLimitMiddleware
from app.schemas import ComposterCreate, ModerateRequest


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -91, 91])
def test_invalid_latitude_fails_closed(value):
    assert math.isinf(distance_m(value, 0, 0, 0))
    with pytest.raises(ValidationError):
        ComposterCreate(name="test", device_id="test", latitude=value, longitude=0)


def test_antipodal_distance_is_finite():
    assert distance_m(90, 180, -90, -180) == pytest.approx(math.pi * 6_371_000)


@pytest.mark.parametrize("box", [
    {"x": "NaN", "y": 0, "width": 0.2, "height": 0.2},
    {"x": 0.9, "y": 0, "width": 0.2, "height": 0.2},
])
def test_invalid_annotation_rejected(box):
    with pytest.raises(ValidationError):
        ModerateRequest(approved=False, annotations=[box])


def test_metrics_do_not_store_arbitrary_urls_or_methods(client):
    observability._metrics.clear()
    for index in range(15):
        assert client.request(f"CUSTOM{index}", f"/missing-{index}").status_code == 404
    assert list(observability._metrics) == [("OTHER", "__unmatched__", 404)]
    assert "missing-" not in observability.metrics_text()


def request(path="/auth/login", forwarded="", direct="127.0.0.1"):
    return Request({"type": "http", "method": "POST", "path": path,
                    "headers": [(b"x-forwarded-for", forwarded.encode())],
                    "client": (direct, 1000)})


def test_proxy_chain_does_not_trust_spoofed_leftmost_ip():
    guard = RateLimitMiddleware(None)
    assert guard._client_ip(request(forwarded="1.2.3.4, 8.8.8.8")) == "8.8.8.8"
    assert guard._client_ip(request(forwarded="1.2.3.4", direct="8.8.8.8")) == "8.8.8.8"
    assert guard._limit(request("/auth/login/")) == (60, 60)


def test_upload_quota_shared_between_resource_ids():
    import asyncio

    async def exercise():
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        guard = RateLimitMiddleware(downstream)
        statuses = []
        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
        for index in range(31):
            await guard(request(f"/sessions/{index}/photo").scope, None, send)
        assert statuses == [204] * 30 + [429]
        assert len(guard._events) == 1
    asyncio.run(exercise())


def test_decompression_bomb_returns_413(monkeypatch):
    def bomb(*args, **kwargs):
        raise Image.DecompressionBombError("too large")
    monkeypatch.setattr(storage.Image, "open", bomb)
    with pytest.raises(HTTPException) as exc:
        storage.validated_image(SimpleNamespace(file=io.BytesIO(b"x" * 128)))
    assert exc.value.status_code == 413


@pytest.mark.parametrize("payload", [[], {"status": "LIKELY_VALID", "confidence": "NaN"}, {"status": "bad", "confidence": 0}])
def test_malformed_ml_response_falls_back_to_manual_review(monkeypatch, payload):
    monkeypatch.setattr(ml.httpx, "post", lambda *a, **kw: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))
    assert ml.request_ml_review("photo.jpg") == {"status": "NEEDS_MANUAL_REVIEW", "confidence": 0.0, "violations": []}


def test_history_has_constant_query_count():
    with SessionLocal() as db:
        for index in range(12):
            session = AccessSession(id=f"history-{index}", user_id="user-1", composter_id="composter-1")
            db.add(session)
            if index % 2:
                db.add(Review(session_id=session.id, photo_key="photo.jpg"))
        db.commit()
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with SessionLocal() as db:
            rows = user_deposits(db, "user-1")
            assert len(rows) == 12
            assert sum(review is not None for _, review in rows) == 6
            assert all(session.composter.name == "Test" for session, _ in rows)
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_stale_refresh_read_cannot_overwrite_a_rotated_token(monkeypatch):
    from app.models import AuthSession, User
    from app.security import create_auth_session, rotate_refresh_token

    with SessionLocal() as db:
        _, refresh = create_auth_session(db.get(User, "user-1"), db, "test")
        stale = db.query(AuthSession).one()
        with SessionLocal() as competing:
            _, _, replacement = rotate_refresh_token(refresh, competing)
        # Simulate a request whose SELECT finished before the winning rotation.
        monkeypatch.setattr(db, "query", lambda *args: SimpleNamespace(
            filter=lambda *args: SimpleNamespace(one_or_none=lambda: stale)))
        with pytest.raises(HTTPException) as exc:
            rotate_refresh_token(refresh, db)
        assert exc.value.status_code == 401
    with SessionLocal() as db:
        assert rotate_refresh_token(replacement, db)[0].id == "user-1"


def test_review_decision_is_idempotent_across_transports(client):
    from app.models import ScoreTransaction, UserScore
    from sqlalchemy import func, select
    from tests.conftest import token

    with SessionLocal() as db:
        db.add(AccessSession(id="decision-session", user_id="user-1", composter_id="composter-1"))
        db.add(Review(id="decision-review", session_id="decision-session", photo_key="test.jpg"))
        db.commit()
    assert client.post("/web/login", data={"email": "moderator@example.com", "password": "Password1!"}, follow_redirects=False).status_code == 303
    response = client.post("/web/reviews/decision-review", data={"action": "APPROVE", "csrf_token": client.cookies.get("compost_csrf")}, follow_redirects=False)
    assert response.status_code == 303
    headers = {"Authorization": f"Bearer {token(client, 'moderator@example.com')}"}
    assert client.post("/moderation/reviews/decision-review", json={"approved": True}, headers=headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(UserScore, "user-1").points == 10
        assert db.scalar(select(func.count(ScoreTransaction.id))) == 1


def test_ml_photo_read_is_bounded_and_body_closed(monkeypatch):
    from ml_runtime import storage as ml_storage
    body = io.BytesIO(b"x" * 200)
    monkeypatch.setattr(ml_storage, "get_settings", lambda: SimpleNamespace(s3_bucket="test", max_photo_bytes=128))
    monkeypatch.setattr(ml_storage, "s3_client", lambda: SimpleNamespace(get_object=lambda **kw: {"Body": body}))
    with pytest.raises(ValueError, match="allowed size"):
        ml_storage.read_photo("photo")
    assert body.closed


def test_web_logout_revokes_the_server_side_session(client):
    assert client.post("/web/login", data={"email": "moderator@example.com", "password": "Password1!"}, follow_redirects=False).status_code == 303
    cookie = client.cookies.get("compost_session")
    csrf = client.cookies.get("compost_csrf")
    with SessionLocal() as db:
        from app.security import user_from_token
        assert user_from_token(cookie, db).id == "mod-1"
    assert client.post("/web/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        from app.security import user_from_token
        with pytest.raises(HTTPException) as exc:
            user_from_token(cookie, db)
        assert exc.value.status_code == 401


def test_delete_composter_preserves_points_and_incidents(client):
    from app.models import Incident, MaintenanceRecord, ProximityChallenge, ScoreTransaction
    from tests.conftest import token

    with SessionLocal() as db:
        db.add(AccessSession(id="delete-session", user_id="user-1", composter_id="composter-1"))
        db.add(Review(id="delete-review", session_id="delete-session", photo_key="photo.jpg"))
        db.add(ScoreTransaction(user_id="user-1", amount=10, balance_after=10, reason="test", review_id="delete-review"))
        db.add(Incident(id="delete-incident", composter_id="composter-1", kind="LOCK", title="test"))
        db.add(MaintenanceRecord(composter_id="composter-1", engineer_id="engineer-1", action="inspection"))
        db.add(ProximityChallenge(user_id="user-1", composter_id="composter-1", command_id="delete-proof", expires_at=1000))
        db.commit()
    headers = {"Authorization": f"Bearer {token(client, 'admin@example.com')}"}
    result = client.post("/admin/composters/composter-1/delete", json={"current_password": "Password1!"}, headers=headers)
    assert result.status_code == 200
    with SessionLocal() as db:
        from sqlalchemy import select
        assert db.scalar(select(Incident).where(Incident.id == "delete-incident")).composter_id is None
        assert db.scalar(select(ScoreTransaction)).review_id is None
        assert db.scalar(select(MaintenanceRecord)) is None
        assert db.scalar(select(ProximityChallenge)) is None


def test_blocked_staff_cannot_use_privileged_api_or_web(client):
    from app.models import User
    from tests.conftest import token

    staff_token = token(client, "moderator@example.com")
    assert client.post("/web/login", data={"email": "moderator@example.com", "password": "Password1!"}, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        db.get(User, "mod-1").is_blocked = True
        db.commit()
    assert client.get("/moderation/reviews", headers={"Authorization": f"Bearer {staff_token}"}).status_code == 403
    assert client.get("/web/dashboard", follow_redirects=False).status_code == 403
    assert client.post("/web/login", data={"email": "moderator@example.com", "password": "Password1!"}, follow_redirects=False).status_code == 401
