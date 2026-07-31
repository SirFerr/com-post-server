import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .admin import router as admin_router
from .database import Base, engine, get_db, migrate_schema
from .models import AccessSession, AuditLog, AuthSession, Composter, DeviceCommand, Review, ReviewStatus, Role, ScoreTransaction, SessionStatus, User, UserScore, Violation
from .schemas import AccessRequest, ChangePasswordRequest, CommandAck, LoginRequest, ModerateRequest, PasswordConfirmation, RefreshRequest, RegisterRequest
from .security import create_auth_session, create_token, current_user, hash_password, require_roles, rotate_refresh_token, signed_command, verify_password
from .services import apply_violation, audit, distance_m, photo_url, request_ml_review, reward_review, store_photo
from .web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    migrate_schema()
    yield


app = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/web"):
        response = RedirectResponse(f"/web/login?next={request.url.path}", status_code=303)
        response.delete_cookie("compost_session")
        response.delete_cookie("compost_csrf")
        return response
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.get("/", include_in_schema=False)
def web_root():
    return RedirectResponse("/web", status_code=307)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    access_token, refresh_token = create_auth_session(user, db, data.device_name)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@app.post("/auth/register", status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email is already registered")
    user = User(email=email, password_hash=hash_password(data.password), full_name=data.full_name.strip(), role=Role.USER)
    db.add(user)
    db.flush()
    audit(db, user, "user", user.id, "USER_REGISTERED")
    db.commit()
    access_token, refresh_token = create_auth_session(user, db, data.email)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@app.post("/auth/refresh")
def refresh(data: RefreshRequest, db: Session = Depends(get_db)):
    user, access_token, refresh_token = rotate_refresh_token(data.refresh_token, db)
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer", "role": user.role}


@app.get("/profile/sessions")
def auth_sessions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).order_by(AuthSession.last_used_at.desc())).all()
    return [{"id": row.id, "device_name": row.device_name, "created_at": row.created_at, "last_used_at": row.last_used_at, "expires_at": row.expires_at} for row in rows]


@app.delete("/profile/sessions/{session_id}")
def revoke_auth_session(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(AuthSession, session_id)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "Session not found")
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "revoked"}


@app.get("/composters")
def composters(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": c.id, "name": c.name, "device_id": c.device_id, "latitude": c.latitude, "longitude": c.longitude, "is_available": c.is_available, "needs_emptying": c.needs_emptying, "qr_payload": f"compost://composter/{c.id}"} for c in db.scalars(select(Composter)).all()]


def refresh_expired_ban(user: User, db: Session) -> None:
    if user.is_blocked and user.ban_until:
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


@app.get("/composters/resolve-qr/{composter_id}")
def resolve_qr(composter_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Unknown composter QR code")
    return {"id": composter.id, "name": composter.name, "device_id": composter.device_id, "latitude": composter.latitude, "longitude": composter.longitude, "is_available": composter.is_available, "needs_emptying": composter.needs_emptying, "qr_payload": f"compost://composter/{composter.id}"}


@app.post("/composters/{composter_id}/report-full")
def report_full(composter_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    require_container_access(actor, db)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.needs_emptying = True
    audit(db, actor, "composter", composter.id, "FULL_REPORTED")
    db.commit()
    return {"status": "reported"}


@app.post("/composters/{composter_id}/access")
def request_access(composter_id: str, data: AccessRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    composter = db.get(Composter, composter_id)
    if not composter or not composter.is_available:
        raise HTTPException(409, "Composter unavailable")
    if distance_m(data.latitude, data.longitude, composter.latitude, composter.longitude) > composter.radius_m:
        raise HTTPException(403, "User is outside the allowed radius")
    now = datetime.now(timezone.utc)
    expired_sessions = db.scalars(
        select(AccessSession)
        .join(User, AccessSession.user_id == User.id)
        .join(DeviceCommand, DeviceCommand.session_id == AccessSession.id)
        .where(
            AccessSession.composter_id == composter.id,
            User.role == Role.USER,
            AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED]),
            DeviceCommand.acknowledged.is_(False),
            DeviceCommand.expires_at < int(now.timestamp()),
        )
    ).all()
    for expired in expired_sessions:
        expired.status = SessionStatus.FAILED
        expired.closed_at = now
    if expired_sessions:
        db.flush()
    # Staff diagnostics use access sessions for signed commands too, but they must
    # never reserve a public composter for regular users.
    active = db.scalar(
        select(AccessSession)
        .join(User, AccessSession.user_id == User.id)
        .where(
            AccessSession.composter_id == composter.id,
            User.role == Role.USER,
            AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED]),
        )
    )
    if active:
        raise HTTPException(409, "Another operation is active")
    session = AccessSession(user_id=user.id, composter_id=composter.id)
    db.add(session)
    db.flush()
    audit(db, user, "composter", composter.id, "OPEN_REQUESTED", {"session_id": session.id})
    payload = signed_command(composter, session.id, "OPEN")
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action="OPEN", nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    db.commit()
    return {"session_id": session.id, "command": payload}


@app.post("/sessions/{session_id}/ack")
def acknowledge(session_id: str, data: CommandAck, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = db.get(AccessSession, session_id)
    command = db.get(DeviceCommand, data.command_id)
    if not session or session.user_id != user.id or not command or command.session_id != session.id:
        raise HTTPException(404, "Session or command not found")
    if command.acknowledged:
        raise HTTPException(409, "Command already acknowledged")
    if data.status not in {"SUCCESS", "LOCK_FAILURE", "INVALID_FORMAT", "WRONG_DEVICE", "UNSUPPORTED_ACTION", "EXPIRED", "REPLAY_DETECTED", "INVALID_SIGNATURE", "TIMEOUT", "CONNECTION_FAILED"}:
        raise HTTPException(422, "Unknown device status")
    command.acknowledged = True
    success = data.status == "SUCCESS"
    diagnostic = session.user.role != Role.USER
    session.status = (
        SessionStatus.CLOSED if diagnostic and success
        else SessionStatus.OPENED if command.action == "OPEN" and success
        else SessionStatus.CLOSED if success
        else SessionStatus.FAILED
    )
    if success:
        session.composter.lock_state = "OPEN" if command.action == "OPEN" else "CLOSED"
        session.composter.last_seen_at = datetime.now(timezone.utc)
    if session.status in {SessionStatus.CLOSED, SessionStatus.FAILED}:
        session.closed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": session.status}


@app.post("/sessions/{session_id}/photo")
def upload_photo(session_id: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    session = db.get(AccessSession, session_id)
    if not session or session.user_id != user.id or session.status != SessionStatus.OPENED:
        raise HTTPException(409, "Session is not ready for a photo")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(415, "Only images are accepted")
    key = store_photo(file)
    ml = request_ml_review(key)
    review = Review(session_id=session.id, photo_key=key, ml_status=ml.get("status", "NEEDS_MANUAL_REVIEW"), ml_confidence=float(ml.get("confidence", 0)), ml_violations=json.dumps(ml.get("violations", []), ensure_ascii=False))
    db.add(review)
    session.status = SessionStatus.CLOSE_REQUESTED
    audit(db, user, "composter", session.composter_id, "CLOSE_REQUESTED", {"session_id": session.id})
    payload = signed_command(session.composter, session.id, "CLOSE")
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action="CLOSE", nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    db.commit()
    return {"review_id": review.id, "command": payload}


@app.get("/sessions/active")
def active_session(db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = db.scalar(
        select(AccessSession)
        .where(
            AccessSession.user_id == user.id,
            AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED]),
        )
        .order_by(AccessSession.created_at.desc())
    )
    if not session:
        return {"session_id": None, "composter_id": None, "status": None, "close_pending": False}
    return {
        "session_id": session.id,
        "composter_id": session.composter_id,
        "status": session.status,
        "close_pending": session.status == SessionStatus.CLOSE_REQUESTED,
    }


@app.post("/sessions/{session_id}/retry-close")
def retry_close(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    session = db.get(AccessSession, session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    if session.status != SessionStatus.CLOSE_REQUESTED:
        raise HTTPException(409, "Session is not waiting for close")
    payload = signed_command(session.composter, session.id, "CLOSE")
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action="CLOSE", nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    db.commit()
    return {"session_id": session.id, "command": payload}


@app.get("/moderation/reviews")
def pending_reviews(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    reviews = db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all()
    result = []
    for review in reviews:
        session = db.get(AccessSession, review.session_id)
        result.append({"id": review.id, "session_id": review.session_id, "photo_url": photo_url(review.photo_key), "status": review.status, "ml_status": review.ml_status, "confidence": review.ml_confidence, "ml_violations": json.loads(review.ml_violations), "annotations": json.loads(review.annotations or "[]"), "created_at": review.created_at, "user_name": session.user.full_name or session.user.email, "user_email": session.user.email, "composter_id": session.composter.id, "composter_name": session.composter.name})
    return result


@app.post("/moderation/reviews/{review_id}")
def moderate(review_id: str, data: ModerateRequest, db: Session = Depends(get_db), moderator: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    if review.status != ReviewStatus.PENDING:
        return {"status": review.status}
    review.status = ReviewStatus.APPROVED if data.approved else ReviewStatus.REJECTED
    comment = (data.comment or "").strip()
    review.comment = comment or (None if data.approved else "Обнаружено нарушение")
    review.annotations = json.dumps(data.annotations, ensure_ascii=False)
    review.reviewed_by = moderator.id
    review.reviewed_at = datetime.now(timezone.utc)
    session = db.get(AccessSession, review.session_id)
    user = db.get(User, session.user_id)
    was_blocked = user.is_blocked
    if not data.approved:
        review.violation_reason = data.violation_reason or "INVALID_COMPOST"
        apply_violation(db, review, session, review.violation_reason)
        if not was_blocked and user.is_blocked:
            audit(db, moderator, "user", user.id, "USER_AUTO_BLOCKED", {"review_id": review.id})
    reward_review(db, session, data.approved)
    audit(db, moderator, "review", review.id, "REVIEW_APPROVED" if data.approved else "REVIEW_REJECTED")
    db.commit()
    return {"status": review.status}


@app.post("/moderation/violations/{violation_id}/cancel")
def cancel_violation(violation_id: str, db: Session = Depends(get_db), moderator: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    violation = db.get(Violation, violation_id)
    if not violation:
        raise HTTPException(404, "Violation not found")
    violation.is_active = False
    db.flush()
    remaining = db.scalars(select(Violation).where(Violation.user_id == violation.user_id, Violation.is_active.is_(True))).all()
    if len(remaining) < 3:
        user = db.get(User, violation.user_id)
        user.is_blocked = False
        user.ban_reason = None
    audit(db, moderator, "violation", violation.id, "VIOLATION_CANCELLED")
    db.commit()
    return {"status": "cancelled"}


@app.get("/profile/score")
def score(db: Session = Depends(get_db), user: User = Depends(current_user)):
    value = db.get(UserScore, user.id)
    transactions = db.scalars(select(ScoreTransaction).where(ScoreTransaction.user_id == user.id).order_by(ScoreTransaction.created_at.desc()).limit(50)).all()
    return {"points": value.points if value else 0, "total_uploads": value.total_uploads if value else 0, "valid_uploads": value.valid_uploads if value else 0, "transactions": [{"id": row.id, "amount": row.amount, "balance_after": row.balance_after, "reason": row.reason, "created_at": row.created_at} for row in transactions]}


@app.get("/profile")
def profile(db: Session = Depends(get_db), user: User = Depends(current_user)):
    refresh_expired_ban(user, db)
    active_violations = db.scalar(select(func.count(Violation.id)).where(Violation.user_id == user.id, Violation.is_active.is_(True))) or 0
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role, "is_blocked": user.is_blocked, "ban_reason": user.ban_reason, "ban_until": user.ban_until, "active_violations": active_violations, "created_at": user.created_at}


@app.post("/profile/change-password")
def change_password(data: ChangePasswordRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(409, "New password must be different")
    user.password_hash = hash_password(data.new_password)
    audit(db, user, "user", user.id, "PASSWORD_CHANGED")
    db.commit()
    return {"status": "changed"}


@app.post("/profile/delete")
def delete_account(data: PasswordConfirmation, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    if user.role == Role.ADMIN:
        admins = db.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN)) or 0
        if admins <= 1:
            raise HTTPException(409, "Last administrator cannot be deleted")
    session_ids = list(db.scalars(select(AccessSession.id).where(AccessSession.user_id == user.id)).all())
    review_ids = list(db.scalars(select(Review.id).where(Review.session_id.in_(session_ids))).all()) if session_ids else []
    if review_ids:
        db.execute(delete(Violation).where(Violation.review_id.in_(review_ids)))
        db.execute(delete(Review).where(Review.id.in_(review_ids)))
    db.execute(delete(Violation).where(Violation.user_id == user.id))
    if session_ids:
        db.execute(delete(DeviceCommand).where(DeviceCommand.session_id.in_(session_ids)))
        db.execute(delete(AccessSession).where(AccessSession.id.in_(session_ids)))
    db.execute(update(Review).where(Review.reviewed_by == user.id).values(reviewed_by=None))
    db.execute(update(AuditLog).where(AuditLog.user_id == user.id).values(user_id=None))
    db.execute(delete(UserScore).where(UserScore.user_id == user.id))
    db.delete(user)
    db.commit()
    return {"status": "deleted"}


@app.get("/profile/deposits")
def deposit_history(db: Session = Depends(get_db), user: User = Depends(current_user)):
    sessions = db.scalars(select(AccessSession).where(AccessSession.user_id == user.id).order_by(AccessSession.created_at.desc()).limit(100)).all()
    result = []
    for session in sessions:
        review = db.scalar(select(Review).where(Review.session_id == session.id))
        moderator_comment = None
        if review:
            moderator_comment = review.comment or ("Обнаружено нарушение" if review.status == ReviewStatus.REJECTED else None)
        result.append({"id": session.id, "composter_name": session.composter.name, "status": session.status, "created_at": session.created_at, "closed_at": session.closed_at, "review_status": review.status if review else None, "ml_status": review.ml_status if review else None, "moderator_comment": moderator_comment, "photo_url": photo_url(review.photo_key) if review else None, "annotations": json.loads(review.annotations or "[]") if review else [], "violation_reason": review.violation_reason if review else None, "reviewed_at": review.reviewed_at if review else None})
    return result
