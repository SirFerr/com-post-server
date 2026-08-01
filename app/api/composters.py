import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AccessSession, Composter, DeviceCommand, Review, Role, SessionStatus, User
from ..schemas import AccessRequest, CommandAck
from ..security import current_user, signed_command
from ..services import audit, distance_m, request_ml_review, store_photo
from .dependencies import require_container_access

router = APIRouter()


def composter_payload(composter: Composter) -> dict:
    return {"id": composter.id, "name": composter.name, "device_id": composter.device_id, "latitude": composter.latitude, "longitude": composter.longitude, "is_available": composter.is_available, "needs_emptying": composter.needs_emptying, "qr_payload": f"compost://composter/{composter.id}"}


@router.get("/composters")
def composters(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [composter_payload(item) for item in db.scalars(select(Composter)).all()]


@router.get("/composters/resolve-qr/{composter_id}")
def resolve_qr(composter_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Unknown composter QR code")
    return composter_payload(composter)


@router.post("/composters/{composter_id}/report-full")
def report_full(composter_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    require_container_access(actor, db)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.needs_emptying = True
    audit(db, actor, "composter", composter.id, "FULL_REPORTED")
    db.commit()
    return {"status": "reported"}


@router.post("/composters/{composter_id}/access")
def request_access(composter_id: str, data: AccessRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    composter = db.get(Composter, composter_id)
    if not composter or not composter.is_available:
        raise HTTPException(409, "Composter unavailable")
    if distance_m(data.latitude, data.longitude, composter.latitude, composter.longitude) > composter.radius_m:
        raise HTTPException(403, "User is outside the allowed radius")
    now = datetime.now(timezone.utc)
    expired_sessions = db.scalars(select(AccessSession).join(User, AccessSession.user_id == User.id).join(DeviceCommand, DeviceCommand.session_id == AccessSession.id).where(AccessSession.composter_id == composter.id, User.role == Role.USER, AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED]), DeviceCommand.acknowledged.is_(False), DeviceCommand.expires_at < int(now.timestamp()))).all()
    for expired in expired_sessions:
        expired.status = SessionStatus.FAILED
        expired.closed_at = now
    if expired_sessions:
        db.flush()
    active = db.scalar(select(AccessSession).join(User, AccessSession.user_id == User.id).where(AccessSession.composter_id == composter.id, User.role == Role.USER, AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED])))
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


@router.post("/sessions/{session_id}/ack")
def acknowledge(session_id: str, data: CommandAck, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = db.get(AccessSession, session_id)
    command = db.get(DeviceCommand, data.command_id)
    if not session or session.user_id != user.id or not command or command.session_id != session.id:
        raise HTTPException(404, "Session or command not found")
    if command.acknowledged:
        raise HTTPException(409, "Command already acknowledged")
    allowed = {"SUCCESS", "LOCK_FAILURE", "INVALID_FORMAT", "WRONG_DEVICE", "UNSUPPORTED_ACTION", "EXPIRED", "REPLAY_DETECTED", "INVALID_SIGNATURE", "TIMEOUT", "CONNECTION_FAILED"}
    if data.status not in allowed:
        raise HTTPException(422, "Unknown device status")
    command.acknowledged = True
    success = data.status == "SUCCESS"
    diagnostic = session.user.role != Role.USER
    session.status = SessionStatus.CLOSED if diagnostic and success else SessionStatus.OPENED if command.action == "OPEN" and success else SessionStatus.CLOSED if success else SessionStatus.FAILED
    if success:
        session.composter.lock_state = "OPEN" if command.action == "OPEN" else "CLOSED"
        session.composter.last_seen_at = datetime.now(timezone.utc)
    if session.status in {SessionStatus.CLOSED, SessionStatus.FAILED}:
        session.closed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": session.status}


@router.post("/sessions/{session_id}/photo")
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


@router.get("/sessions/active")
def active_session(db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = db.scalar(select(AccessSession).where(AccessSession.user_id == user.id, AccessSession.status.not_in([SessionStatus.CLOSED, SessionStatus.FAILED])).order_by(AccessSession.created_at.desc()))
    if not session:
        return {"session_id": None, "composter_id": None, "status": None, "close_pending": False}
    return {"session_id": session.id, "composter_id": session.composter_id, "status": session.status, "close_pending": session.status == SessionStatus.CLOSE_REQUESTED}


@router.post("/sessions/{session_id}/retry-close")
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
