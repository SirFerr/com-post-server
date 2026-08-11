import base64
import hashlib
import hmac
import json
import os
import time
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AuthSession, Composter, Role, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def create_token(user: User, session_id: str | None = None, *, ttl_minutes: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    token_ttl_minutes = settings.jwt_ttl_minutes if ttl_minutes is None else ttl_minutes
    payload = {
        "sub": user.id,
        "role": user.role.value,
        "ver": user.token_version,
        "iat": now,
        "exp": now + timedelta(minutes=token_ttl_minutes),
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_auth_session(user: User, db: Session, device_name: str) -> tuple[str, str]:
    raw_refresh = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    session = AuthSession(
        user_id=user.id,
        refresh_token_hash=hashlib.sha256(raw_refresh.encode()).hexdigest(),
        device_name=device_name.strip() or "Unknown device",
        expires_at=now + timedelta(days=get_settings().refresh_ttl_days),
    )
    db.add(session)
    db.commit()
    return create_token(user, session.id), raw_refresh


def rotate_refresh_token(raw_refresh: str, db: Session) -> tuple[User, str, str]:
    digest = hashlib.sha256(raw_refresh.encode()).hexdigest()
    session = db.query(AuthSession).filter(AuthSession.refresh_token_hash == digest).one_or_none()
    now = datetime.now(timezone.utc)
    if not session or session.revoked_at is not None or session.expires_at.replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(401, "Invalid refresh token")
    user = db.get(User, session.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(401, "User not found")
    replacement = secrets.token_urlsafe(48)
    session.refresh_token_hash = hashlib.sha256(replacement.encode()).hexdigest()
    session.last_used_at = now
    db.commit()
    return user, create_token(user, session.id), replacement


def user_from_token(token: str, db: Session) -> User:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Access token expired")
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(401, "Invalid access token")
    user = db.get(User, user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(401, "User not found")
    if payload.get("ver") != user.token_version:
        raise HTTPException(401, "Access token has been revoked")
    session_id = payload.get("sid")
    if session_id:
        session = db.get(AuthSession, session_id)
        now = datetime.now(timezone.utc)
        if (
            not session
            or session.user_id != user.id
            or session.revoked_at is not None
            or session.expires_at.replace(tzinfo=timezone.utc) <= now
        ):
            raise HTTPException(401, "Access token has been revoked")
    return user


def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    return user_from_token(token, db)


def web_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("compost_session")
    if not token:
        raise HTTPException(401, "Authentication required")
    user = user_from_token(token, db)
    if user.role == Role.USER:
        raise HTTPException(403, "Staff account required")
    return user


def require_roles(*roles: Role):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Insufficient permissions")
        return user
    return dependency


def signed_command(composter: Composter, session_id: str, action: str) -> dict:
    issued = int(time.time())
    data = {
        "v": 1,
        "commandId": os.urandom(16).hex(),
        "composterId": composter.id,
        "action": action,
        "issuedAt": issued,
        "expiresAt": issued + get_settings().command_ttl_seconds,
        "nonce": base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("="),
        "sessionId": session_id,
    }
    canonical = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    data["signature"] = hmac.new(composter.secret.encode(), canonical, hashlib.sha256).hexdigest()
    return data


def verify_device_response(
    composter: Composter,
    command_id: str,
    status: str,
    state: str,
    device_id: str,
    signature: str,
) -> bool:
    if device_id != composter.device_id or state not in {"OPEN", "CLOSED"}:
        return False
    payload = {
        "commandId": command_id,
        "deviceId": device_id,
        "state": state,
        "status": status,
        "v": 1,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    expected = hmac.new(composter.secret.encode(), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
