import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AccessSession, AuditLog, AuthSession, DeviceCommand, Review, ReviewStatus, Role, ScoreTransaction, User, UserScore, Violation
from ..schemas import ChangePasswordRequest, LoginRequest, PasswordConfirmation, RefreshRequest, RegisterRequest
from ..security import create_auth_session, current_user, hash_password, rotate_refresh_token, verify_password
from ..services import audit, photo_url
from .dependencies import refresh_expired_ban

router = APIRouter()


@router.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower(), User.deleted_at.is_(None)))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    access_token, refresh_token = create_auth_session(user, db, data.device_name)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@router.post("/auth/register", status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email, User.deleted_at.is_(None))):
        raise HTTPException(409, "Email is already registered")
    user = User(email=email, password_hash=hash_password(data.password), full_name=data.full_name.strip(), role=Role.USER)
    db.add(user)
    db.flush()
    audit(db, user, "user", user.id, "USER_REGISTERED")
    db.commit()
    access_token, refresh_token = create_auth_session(user, db, data.email)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@router.post("/auth/refresh")
def refresh(data: RefreshRequest, db: Session = Depends(get_db)):
    user, access_token, refresh_token = rotate_refresh_token(data.refresh_token, db)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@router.get("/profile/sessions")
def auth_sessions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).order_by(AuthSession.last_used_at.desc())).all()
    return [{"id": row.id, "device_name": row.device_name, "created_at": row.created_at, "last_used_at": row.last_used_at, "expires_at": row.expires_at} for row in rows]


@router.delete("/profile/sessions/{session_id}")
def revoke_auth_session(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(AuthSession, session_id)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "Session not found")
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "revoked"}


@router.get("/profile/score")
def score(db: Session = Depends(get_db), user: User = Depends(current_user)):
    value = db.get(UserScore, user.id)
    transactions = db.scalars(select(ScoreTransaction).where(ScoreTransaction.user_id == user.id).order_by(ScoreTransaction.created_at.desc()).limit(50)).all()
    return {"points": value.points if value else 0, "total_uploads": value.total_uploads if value else 0, "valid_uploads": value.valid_uploads if value else 0, "transactions": [{"id": row.id, "amount": row.amount, "balance_after": row.balance_after, "reason": row.reason, "created_at": row.created_at} for row in transactions]}


@router.get("/profile")
def profile(db: Session = Depends(get_db), user: User = Depends(current_user)):
    refresh_expired_ban(user, db)
    active_violations = db.scalar(select(func.count(Violation.id)).where(Violation.user_id == user.id, Violation.is_active.is_(True))) or 0
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role, "is_blocked": user.is_blocked, "ban_reason": user.ban_reason, "ban_until": user.ban_until, "active_violations": active_violations, "created_at": user.created_at}


@router.post("/profile/change-password")
def change_password(data: ChangePasswordRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(409, "New password must be different")
    user.password_hash = hash_password(data.new_password)
    user.token_version += 1
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    audit(db, user, "user", user.id, "PASSWORD_CHANGED")
    db.commit()
    return {"status": "changed"}


@router.post("/profile/delete")
def delete_account(data: PasswordConfirmation, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    if user.role == Role.ADMIN:
        admins = db.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN)) or 0
        if admins <= 1:
            raise HTTPException(409, "Last administrator cannot be deleted")
    # Preserve immutable moderation/training history and its foreign keys, but
    # immediately remove credentials and personally identifying profile data.
    now = datetime.now(timezone.utc)
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=now))
    db.execute(update(Review).where(Review.reviewed_by == user.id).values(reviewed_by=None))
    db.execute(update(AuditLog).where(AuditLog.user_id == user.id).values(user_id=None))
    db.execute(delete(UserScore).where(UserScore.user_id == user.id))
    user.email = f"deleted-{user.id}-{secrets.token_hex(4)}@invalid.local"
    user.full_name = "Deleted user"
    user.password_hash = hash_password(secrets.token_urlsafe(48))
    user.warning_message = None
    user.ban_reason = None
    user.ban_until = None
    user.is_blocked = True
    user.token_version += 1
    user.deleted_at = now
    db.commit()
    return {"status": "deleted"}


@router.get("/profile/deposits")
def deposit_history(db: Session = Depends(get_db), user: User = Depends(current_user)):
    sessions = db.scalars(select(AccessSession).where(AccessSession.user_id == user.id).order_by(AccessSession.created_at.desc()).limit(100)).all()
    result = []
    for session in sessions:
        review = db.scalar(select(Review).where(Review.session_id == session.id))
        moderator_comment = None
        if review:
            moderator_comment = review.comment or (
                "Обнаружено нарушение"
                if review.status == ReviewStatus.REJECTED
                else None
            )
        result.append(
            {
                "id": session.id,
                "composter_name": session.composter.name,
                "status": session.status,
                "created_at": session.created_at,
                "closed_at": session.closed_at,
                "review_status": review.status if review else None,
                "ml_status": review.ml_status if review else None,
                "moderator_comment": moderator_comment,
                "photo_url": photo_url(review.photo_key) if review else None,
                "annotations": json.loads(review.annotations or "[]") if review else [],
                "violation_reason": review.violation_reason if review else None,
                "reviewed_at": review.reviewed_at if review else None,
            }
        )
    return result
