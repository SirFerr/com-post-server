from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import User
from ..services import audit


def refresh_expired_ban(user: User, db: Session) -> None:
    if not user.is_blocked or not user.ban_until:
        return
    ban_until = user.ban_until
    if ban_until.tzinfo is None:
        ban_until = ban_until.replace(tzinfo=timezone.utc)
    if ban_until <= datetime.now(timezone.utc):
        user.is_blocked = False
        user.ban_reason = None
        user.ban_until = None
        audit(db, None, "user", user.id, "USER_BAN_EXPIRED")
        db.commit()


def require_container_access(user: User, db: Session) -> None:
    refresh_expired_ban(user, db)
    if user.is_blocked:
        raise HTTPException(403, "User is blocked")
