import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .admin import router as admin_router
from .database import Base, engine, get_db
from .models import AccessSession, Composter, DeviceCommand, Review, ReviewStatus, Role, SessionStatus, User, UserScore, Violation
from .schemas import AccessRequest, CommandAck, LoginRequest, ModerateRequest, RegisterRequest
from .security import create_token, current_user, hash_password, require_roles, signed_command, verify_password
from .services import apply_violation, audit, distance_m, photo_url, request_ml_review, reward_review, store_photo
from .web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role}


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
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role}


@app.get("/composters")
def composters(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": c.id, "name": c.name, "device_id": c.device_id, "latitude": c.latitude, "longitude": c.longitude, "is_available": c.is_available, "fill_level": c.fill_level, "qr_payload": f"compost://composter/{c.id}"} for c in db.scalars(select(Composter)).all()]


@app.get("/composters/resolve-qr/{composter_id}")
def resolve_qr(composter_id: str, db: Session = Depends(get_db), _: User = Depends(current_user)):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Unknown composter QR code")
    return {"id": composter.id, "name": composter.name, "device_id": composter.device_id, "latitude": composter.latitude, "longitude": composter.longitude, "is_available": composter.is_available, "fill_level": composter.fill_level, "qr_payload": f"compost://composter/{composter.id}"}


@app.post("/composters/{composter_id}/access")
def request_access(composter_id: str, data: AccessRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.is_blocked:
        raise HTTPException(403, "User is blocked")
    composter = db.get(Composter, composter_id)
    if not composter or not composter.is_available:
        raise HTTPException(409, "Composter unavailable")
    if distance_m(data.latitude, data.longitude, composter.latitude, composter.longitude) > composter.radius_m:
        raise HTTPException(403, "User is outside the allowed radius")
    active = db.scalar(select(AccessSession).where(AccessSession.composter_id == composter.id, AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED])))
    if active:
        raise HTTPException(409, "Another operation is active")
    session = AccessSession(user_id=user.id, composter_id=composter.id)
    db.add(session)
    db.flush()
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
    session.status = SessionStatus.OPENED if command.action == "OPEN" and success else SessionStatus.CLOSED if success else SessionStatus.FAILED
    if success:
        session.composter.lock_state = "OPEN" if command.action == "OPEN" else "CLOSED"
        session.composter.last_seen_at = datetime.now(timezone.utc)
    if session.status == SessionStatus.CLOSED:
        session.closed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": session.status}


@app.post("/sessions/{session_id}/photo")
def upload_photo(session_id: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
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
    payload = signed_command(session.composter, session.id, "CLOSE")
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action="CLOSE", nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    db.commit()
    return {"review_id": review.id, "command": payload}


@app.get("/moderation/reviews")
def pending_reviews(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    reviews = db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all()
    result = []
    for review in reviews:
        session = db.get(AccessSession, review.session_id)
        result.append({"id": review.id, "session_id": review.session_id, "photo_url": photo_url(review.photo_key), "status": review.status, "ml_status": review.ml_status, "confidence": review.ml_confidence, "ml_violations": json.loads(review.ml_violations), "created_at": review.created_at, "user_name": session.user.full_name or session.user.email, "user_email": session.user.email, "composter_name": session.composter.name})
    return result


@app.post("/moderation/reviews/{review_id}")
def moderate(review_id: str, data: ModerateRequest, db: Session = Depends(get_db), moderator: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    if review.status != ReviewStatus.PENDING:
        return {"status": review.status}
    review.status = ReviewStatus.APPROVED if data.approved else ReviewStatus.REJECTED
    review.comment = data.comment
    review.reviewed_by = moderator.id
    session = db.get(AccessSession, review.session_id)
    if not data.approved:
        review.violation_reason = data.violation_reason or "INVALID_COMPOST"
        apply_violation(db, review, session, review.violation_reason)
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
    return {"points": value.points if value else 0, "total_uploads": value.total_uploads if value else 0, "valid_uploads": value.valid_uploads if value else 0}


@app.get("/profile")
def profile(db: Session = Depends(get_db), user: User = Depends(current_user)):
    active_violations = db.scalar(select(func.count(Violation.id)).where(Violation.user_id == user.id, Violation.is_active.is_(True))) or 0
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role, "is_blocked": user.is_blocked, "ban_reason": user.ban_reason, "active_violations": active_violations, "created_at": user.created_at}


@app.get("/profile/deposits")
def deposit_history(db: Session = Depends(get_db), user: User = Depends(current_user)):
    sessions = db.scalars(select(AccessSession).where(AccessSession.user_id == user.id).order_by(AccessSession.created_at.desc()).limit(100)).all()
    result = []
    for session in sessions:
        review = db.scalar(select(Review).where(Review.session_id == session.id))
        result.append({"id": session.id, "composter_name": session.composter.name, "status": session.status, "created_at": session.created_at, "closed_at": session.closed_at, "review_status": review.status if review else None, "ml_status": review.ml_status if review else None, "moderator_comment": review.comment if review else None})
    return result
