import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import AccessSession, AuditLog, Composter, DeviceCommand, Review, ReviewStatus, Role, SessionStatus, Telemetry, User, Violation
from .schemas import ComposterCreate, ComposterUpdate, DebugCommandRequest, TelemetryRequest, UserAdminUpdate
from .security import require_roles, signed_command
from .services import audit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "blocked_users": db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0,
        "composters": db.scalar(select(func.count(Composter.id))) or 0,
        "available_composters": db.scalar(select(func.count(Composter.id)).where(Composter.is_available.is_(True))) or 0,
        "sessions": db.scalar(select(func.count(AccessSession.id))) or 0,
        "pending_reviews": db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.PENDING)) or 0,
        "active_violations": db.scalar(select(func.count(Violation.id)).where(Violation.is_active.is_(True))) or 0,
    }


@router.get("/users")
def users(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    return [{"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role, "is_blocked": u.is_blocked, "ban_reason": u.ban_reason, "created_at": u.created_at} for u in db.scalars(select(User).order_by(User.created_at.desc())).all()]


@router.patch("/users/{user_id}")
def update_user(user_id: str, data: UserAdminUpdate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if data.role is not None:
        try:
            user.role = Role(data.role)
        except ValueError:
            raise HTTPException(422, "Unknown role")
    if data.is_blocked is not None:
        user.is_blocked = data.is_blocked
        user.ban_reason = data.ban_reason if data.is_blocked else None
    audit(db, actor, "user", user.id, "USER_UPDATED", data.model_dump(exclude_none=True))
    db.commit()
    return {"id": user.id, "role": user.role, "is_blocked": user.is_blocked, "ban_reason": user.ban_reason}


@router.get("/composters")
def composters(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    return [{"id": c.id, "name": c.name, "device_id": c.device_id, "latitude": c.latitude, "longitude": c.longitude, "radius_m": c.radius_m, "is_available": c.is_available, "fill_level": c.fill_level, "battery_level": c.battery_level, "lock_state": c.lock_state, "last_seen_at": c.last_seen_at, "qr_payload": f"compost://composter/{c.id}"} for c in db.scalars(select(Composter)).all()]


@router.post("/composters")
def create_composter(data: ComposterCreate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    device_secret = data.secret or secrets.token_hex(32)
    composter = Composter(**data.model_dump(exclude={"secret"}), secret=device_secret)
    db.add(composter)
    db.flush()
    audit(db, actor, "composter", composter.id, "COMPOSTER_CREATED")
    db.commit()
    return {"id": composter.id, "qr_payload": f"compost://composter/{composter.id}", "device_secret": device_secret}


@router.patch("/composters/{composter_id}")
def update_composter(composter_id: str, data: ComposterUpdate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(composter, key, value)
    audit(db, actor, "composter", composter.id, "COMPOSTER_UPDATED", data.model_dump(exclude_none=True))
    db.commit()
    return {"id": composter.id, "is_available": composter.is_available}


@router.post("/composters/{composter_id}/debug-command")
def debug_command(composter_id: str, data: DebugCommandRequest, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    action = data.action.upper()
    if action not in {"OPEN", "CLOSE"}:
        raise HTTPException(422, "Only OPEN and CLOSE diagnostics are supported")
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    session = AccessSession(
        user_id=actor.id,
        composter_id=composter.id,
        status=SessionStatus.OPEN_REQUESTED if action == "OPEN" else SessionStatus.CLOSE_REQUESTED,
    )
    db.add(session)
    db.flush()
    payload = signed_command(composter, session.id, action)
    db.add(DeviceCommand(id=payload["commandId"], session_id=session.id, action=action, nonce=payload["nonce"], issued_at=payload["issuedAt"], expires_at=payload["expiresAt"]))
    audit(db, actor, "composter", composter.id, f"DEBUG_{action}_REQUESTED", {"session_id": session.id})
    db.commit()
    return {"session_id": session.id, "command": payload}


@router.post("/composters/{composter_id}/telemetry")
def telemetry(composter_id: str, data: TelemetryRequest, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.battery_level, composter.fill_level, composter.lock_state = data.battery_level, data.fill_level, data.lock_state
    composter.last_seen_at = datetime.now(timezone.utc)
    db.add(Telemetry(composter_id=composter.id, **data.model_dump()))
    audit(db, actor, "composter", composter.id, "TELEMETRY_RECORDED", data.model_dump())
    db.commit()
    return {"status": "recorded"}


@router.get("/audit")
def audit_log(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    return [{"id": row.id, "entity": row.entity, "entity_id": row.entity_id, "action": row.action, "user_id": row.user_id, "details": row.details, "created_at": row.created_at} for row in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500)).all()]
