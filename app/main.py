import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .admin import router as admin_router
from .database import Base, engine, get_db
from .models import AccessSession, Composter, DeviceCommand, Review, ReviewStatus, Role, SessionStatus, User, UserScore, Violation
from .schemas import AccessRequest, CommandAck, LoginRequest, ModerateRequest
from .security import create_token, current_user, require_roles, signed_command, verify_password
from .services import apply_violation, audit, distance_m, photo_url, request_ml_review, reward_review, store_photo


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Community Compost API", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
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
    command.acknowledged = True
    success = data.status == "SUCCESS"
    session.status = SessionStatus.OPENED if command.action == "OPEN" and success else SessionStatus.CLOSED if success else SessionStatus.FAILED
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
    return [{"id": r.id, "session_id": r.session_id, "photo_url": photo_url(r.photo_key), "status": r.status, "ml_status": r.ml_status, "confidence": r.ml_confidence, "ml_violations": json.loads(r.ml_violations)} for r in db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING)).all()]


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
