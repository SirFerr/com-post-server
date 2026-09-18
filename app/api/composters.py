import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..domain.incidents import set_full_state_from_incident
from ..models import AccessSession, Composter, DeviceCommand, Incident, ProximityChallenge, Review, Role, SessionStatus, User
from ..schemas import AccessRequest, CommandAck, DeviceProof
from ..security import current_user, signed_command, verify_device_response
from ..services import audit, distance_m, incident_title, photo_url, request_ml_review, store_photo
from .dependencies import require_container_access

router = APIRouter()


def consume_proximity_proof(
    db: Session,
    actor: User,
    composter: Composter,
    challenge_id: str,
    proof: DeviceProof,
) -> None:
    challenge = db.scalar(
        select(ProximityChallenge)
        .where(ProximityChallenge.id == challenge_id)
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        not challenge
        or challenge.user_id != actor.id
        or challenge.composter_id != composter.id
        or challenge.command_id != proof.command_id
        or challenge.used_at is not None
        or challenge.expires_at < int(now.timestamp())
    ):
        raise HTTPException(403, "Proximity proof is invalid or expired")
    if proof.status != "PROXIMITY_CONFIRMED" or not verify_device_response(
        composter,
        proof.command_id,
        proof.status,
        proof.state,
        proof.device_id,
        proof.signature,
    ):
        raise HTTPException(403, "Proximity proof was not produced by this composter")
    challenge.used_at = now


@router.post("/composters/{composter_id}/proximity-challenge")
def proximity_challenge(
    composter_id: str,
    db: Session = Depends(get_db),
    actor: User = Depends(current_user),
):
    require_container_access(actor, db)
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter or not composter.is_available:
        raise HTTPException(409, "Composter unavailable")
    now = int(datetime.now(timezone.utc).timestamp())
    db.execute(delete(ProximityChallenge).where(ProximityChallenge.expires_at < now - 86_400))
    challenge_id = str(uuid.uuid4())
    command = signed_command(composter, challenge_id, "PROVE")
    challenge = ProximityChallenge(
        id=challenge_id,
        user_id=actor.id,
        composter_id=composter.id,
        command_id=command["commandId"],
        expires_at=command["expiresAt"],
    )
    db.add(challenge)
    db.commit()
    return {"challenge_id": challenge.id, "command": command}


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
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        raise HTTPException(404, "Composter not found")
    incident = set_full_state_from_incident(db, actor, composter, True)
    db.commit()
    return {"status": "reported", "incident_id": incident.id if incident else None}


@router.post("/composters/{composter_id}/incident-report")
def report_incident(
    composter_id: str,
    kind: str = Form("OTHER"),
    comment: str = Form(""),
    latitude: float = Form(..., ge=-90, le=90, allow_inf_nan=False),
    longitude: float = Form(..., ge=-180, le=180, allow_inf_nan=False),
    challenge_id: str = Form(...),
    proof_command_id: str = Form(...),
    proof_status: str = Form(...),
    proof_state: str = Form(...),
    proof_device_id: str = Form(...),
    proof_signature: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: User = Depends(current_user),
):
    require_container_access(actor, db)
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        raise HTTPException(404, "Composter not found")
    if distance_m(latitude, longitude, composter.latitude, composter.longitude) > 50:
        raise HTTPException(403, "User is farther than 50 meters from the composter")
    consume_proximity_proof(db, actor, composter, challenge_id, DeviceProof(
        command_id=proof_command_id,
        status=proof_status,
        state=proof_state,
        device_id=proof_device_id,
        signature=proof_signature,
    ))
    kind = kind.strip().upper()
    if kind not in {"CONTAMINATION", "OVERFLOW", "LOCK", "DEVICE", "MAINTENANCE", "OTHER"}:
        raise HTTPException(422, "Unsupported incident type")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(415, "Only images are accepted")
    comment = comment.strip()
    if len(comment) > 5000:
        raise HTTPException(422, "Comment is too long")
    photo_key = store_photo(file)
    incident = Incident(
        composter_id=composter.id,
        kind=kind,
        severity="MEDIUM",
        title=incident_title(kind),
        description=comment,
        photo_key=photo_key,
        created_by=actor.id,
    )
    db.add(incident)
    db.flush()
    if incident.kind == "OVERFLOW":
        composter.needs_emptying = True
    details = {"incident_id": incident.id, "kind": incident.kind, "photo_key": photo_key, "latitude": latitude, "longitude": longitude}
    audit(db, actor, "incident", incident.id, "INCIDENT_CREATED", details)
    audit(db, actor, "composter", composter.id, "INCIDENT_CREATED", details)
    db.commit()
    return {"id": incident.id, "status": incident.status, "photo_url": photo_url(photo_key)}


@router.post("/composters/{composter_id}/access")
def request_access(composter_id: str, data: AccessRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_container_access(user, db)
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter or not composter.is_available:
        raise HTTPException(409, "Composter unavailable")
    if distance_m(data.latitude, data.longitude, composter.latitude, composter.longitude) > composter.radius_m:
        raise HTTPException(403, "User is outside the allowed radius")
    consume_proximity_proof(db, user, composter, data.challenge_id, data.proof)
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
    session = db.scalar(select(AccessSession).where(AccessSession.id == session_id).with_for_update())
    command = db.scalar(select(DeviceCommand).where(DeviceCommand.id == data.command_id).with_for_update())
    if not session or session.user_id != user.id or not command or command.session_id != session.id:
        raise HTTPException(404, "Session or command not found")
    if command.acknowledged:
        raise HTTPException(409, "Command already acknowledged")
    allowed = {"SUCCESS", "LOCK_FAILURE", "INVALID_FORMAT", "WRONG_DEVICE", "UNSUPPORTED_ACTION", "EXPIRED", "REPLAY_DETECTED", "INVALID_SIGNATURE", "TIMEOUT", "CONNECTION_FAILED"}
    if data.status not in allowed:
        raise HTTPException(422, "Unknown device status")
    if data.status not in {"TIMEOUT", "CONNECTION_FAILED"}:
        if not all((data.state, data.device_id, data.signature)) or not verify_device_response(
            session.composter,
            data.command_id,
            data.status,
            data.state or "",
            data.device_id or "",
            data.signature or "",
        ):
            raise HTTPException(403, "Device response signature is invalid")
        if data.status == "SUCCESS" and data.state != ("OPEN" if command.action == "OPEN" else "CLOSED"):
            raise HTTPException(403, "Device state does not match the acknowledged command")
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
    session = db.scalar(select(AccessSession).where(AccessSession.id == session_id).with_for_update())
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
    session = db.scalar(select(AccessSession).where(AccessSession.id == session_id).with_for_update())
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    if session.status != SessionStatus.CLOSE_REQUESTED:
        raise HTTPException(409, "Session is not waiting for close")
    payload = signed_command(session.composter, session.id, "CLOSE")
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action="CLOSE", nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    db.commit()
    return {"session_id": session.id, "command": payload}
