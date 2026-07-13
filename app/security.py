import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Composter, Role, User

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


def create_token(user: User) -> str:
    settings = get_settings()
    payload = {"sub": user.id, "role": user.role.value, "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_ttl_minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def user_from_token(token: str, db: Session) -> User:
    try:
        user_id = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])["sub"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(401, "Invalid access token")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(401, "User not found")
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
