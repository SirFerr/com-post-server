import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from .database import get_db
from .domain.scores import locked_score
from .domain.incidents import audit_incident_resolved, set_full_state_from_incident, sync_full_state_after_incident_change
from .ml_dataset import freeze_dataset_version
from .models import AccessSession, AuditLog, AuthSession, Composter, DatasetVersion, DeviceCommand, Incident, MaintenanceRecord, OutboxEvent, ProximityChallenge, Review, ReviewStatus, Role, ScoreTransaction, SessionStatus, Telemetry, User, Violation
from .schemas import ComposterCreate, ComposterUpdate, DebugCommandRequest, FullStateRequest, IncidentCreate, IncidentUpdate, MaintenanceCreate, MaintenanceModeRequest, PasswordConfirmation, ScoreAdjustment, TelemetryRequest, UserAdminUpdate
from .security import require_roles, signed_command, verify_password
from .services import audit, incident_title, photo_url, store_photo

router = APIRouter(prefix="/admin", tags=["admin"])


def confirm_password(actor: User, password: str) -> None:
    if not verify_password(password, actor.password_hash):
        raise HTTPException(403, "Password confirmation failed")


def history_media(db: Session, row: AuditLog) -> dict:
    if row.entity == "review":
        review = db.get(Review, row.entity_id)
        if review:
            session = db.get(AccessSession, review.session_id)
            composter = db.get(Composter, session.composter_id) if session else None
            return {
                "photo_url": photo_url(review.photo_key),
                "annotations": json.loads(review.annotations or "[]"),
                "composter_id": composter.id if composter else None,
                "composter_name": composter.name if composter else None,
            }
    if row.entity == "composter" and row.action == "CONTAMINATION_REPORTED":
        details = json.loads(row.details or "{}")
        incident = db.get(Incident, details.get("incident_id")) if details.get("incident_id") else None
        composter = db.get(Composter, row.entity_id)
        if incident and incident.photo_key:
            return {
                "photo_url": photo_url(incident.photo_key),
                "annotations": [],
                "composter_id": composter.id if composter else None,
                "composter_name": composter.name if composter else None,
            }
    return {"photo_url": None, "annotations": [], "composter_id": None, "composter_name": None}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    return {
        "users": db.scalar(select(func.count(User.id)).where(User.deleted_at.is_(None))) or 0,
        "blocked_users": db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True), User.deleted_at.is_(None))) or 0,
        "composters": db.scalar(select(func.count(Composter.id))) or 0,
        "available_composters": db.scalar(select(func.count(Composter.id)).where(Composter.is_available.is_(True))) or 0,
        "sessions": db.scalar(select(func.count(AccessSession.id))) or 0,
        "pending_reviews": db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.PENDING)) or 0,
        "active_violations": (db.scalar(select(func.count(Violation.id)).where(Violation.is_active.is_(True))) or 0)
        + (db.scalar(select(func.count(Incident.id)).where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))) or 0),
        "open_incidents": db.scalar(select(func.count(Incident.id)).where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))) or 0,
    }


@router.get("/users")
def users(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR))):
    query = select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc()).offset(offset).limit(limit)
    return [{"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role, "is_blocked": u.is_blocked, "ban_reason": u.ban_reason, "created_at": u.created_at} for u in db.scalars(query).all()]


@router.get("/violations")
def violations(active_only: bool = True, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR))):
    query = select(Violation).order_by(Violation.created_at.desc())
    if active_only:
        query = query.where(Violation.is_active.is_(True))
    rows = list(db.scalars(query.offset(offset).limit(limit)).all())
    actor_ids = {value for row in rows for value in (row.user_id, row.created_by, row.resolved_by) if value}
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    reviews = {review.id: review for review in db.scalars(select(Review).where(Review.id.in_({row.review_id for row in rows}))).all()} if rows else {}
    sessions = {session.id: session for session in db.scalars(select(AccessSession).where(AccessSession.id.in_({review.session_id for review in reviews.values()}))).all()} if reviews else {}
    composter_ids = {session.composter_id for session in sessions.values()}
    composters = {composter.id: composter for composter in db.scalars(select(Composter).where(Composter.id.in_(composter_ids))).all()} if composter_ids else {}
    result = [{
        "id": row.id,
        "kind": "USER_VIOLATION",
        "user_id": row.user_id,
        "user_email": users[row.user_id].email if row.user_id in users else "Удалённый пользователь",
        "review_id": row.review_id,
        "composter_id": sessions[reviews[row.review_id].session_id].composter_id if row.review_id in reviews and reviews[row.review_id].session_id in sessions else None,
        "composter_name": composters[sessions[reviews[row.review_id].session_id].composter_id].name if row.review_id in reviews and reviews[row.review_id].session_id in sessions and sessions[reviews[row.review_id].session_id].composter_id in composters else None,
        "reason": row.reason,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "source_name": (users[row.created_by].full_name or users[row.created_by].email) if row.created_by in users else "Система",
        "resolved_by": row.resolved_by,
        "resolver_name": (users[row.resolved_by].full_name or users[row.resolved_by].email) if row.resolved_by in users else None,
        "resolved_at": row.resolved_at,
        "photo_url": photo_url(reviews[row.review_id].photo_key) if row.review_id in reviews else None,
        "annotations": json.loads(reviews[row.review_id].annotations or "[]") if row.review_id in reviews else [],
    } for row in rows]
    incident_query = select(Incident).order_by(Incident.created_at.desc())
    if active_only:
        incident_query = incident_query.where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))
    incidents = list(db.scalars(incident_query).all())
    incident_actor_ids = {value for row in incidents for value in (row.created_by, row.resolved_by) if value}
    incident_users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(incident_actor_ids))).all()} if incident_actor_ids else {}
    result.extend({
        "id": row.id,
        "kind": row.kind,
        "user_id": None,
        "user_email": None,
        "review_id": None,
        "composter_id": row.composter_id,
        "reason": row.description or row.title,
        "is_active": row.status in ("OPEN", "IN_PROGRESS"),
        "created_at": row.created_at,
        "created_by": row.created_by,
        "source_name": (incident_users[row.created_by].full_name or incident_users[row.created_by].email) if row.created_by in incident_users else "Система",
        "resolved_by": row.resolved_by,
        "resolver_name": (incident_users[row.resolved_by].full_name or incident_users[row.resolved_by].email) if row.resolved_by in incident_users else None,
        "resolved_at": row.resolved_at,
        "photo_url": photo_url(row.photo_key) if row.photo_key else None,
        "annotations": [],
    } for row in incidents)
    return sorted(result, key=lambda item: item["created_at"], reverse=True)


@router.post("/violations/{violation_id}/resolve")
def resolve_violation(violation_id: str, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    violation = db.get(Violation, violation_id)
    if not violation:
        incident = db.get(Incident, violation_id)
        if not incident:
            raise HTTPException(404, "Violation not found")
        incident.status = "RESOLVED"
        incident.resolved_by = actor.id
        incident.resolved_at = datetime.now(timezone.utc)
        db.flush()
        if incident.kind == "OVERFLOW":
            sync_full_state_after_incident_change(db, incident.composter_id)
        audit_incident_resolved(db, actor, incident, "ADMIN_API")
        db.commit()
        return {"status": "resolved"}
    violation.is_active = False
    violation.resolved_by = actor.id
    violation.resolved_at = datetime.now(timezone.utc)
    db.flush()
    remaining = db.scalar(select(func.count(Violation.id)).where(Violation.user_id == violation.user_id, Violation.is_active.is_(True))) or 0
    user = db.get(User, violation.user_id)
    if user and remaining < 3 and user.ban_reason == "Three active composting violations":
        user.is_blocked = False
        user.ban_reason = None
        user.ban_until = None
    audit(db, actor, "violation", violation.id, "VIOLATION_RESOLVED", {"remaining": remaining})
    db.commit()
    return {"status": "resolved"}


@router.get("/users/{user_id}/history")
def user_history(user_id: str, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR))):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    review_ids = list(db.scalars(
        select(Review.id).join(AccessSession, Review.session_id == AccessSession.id).where(AccessSession.user_id == user_id)
    ).all())
    conditions = [and_(AuditLog.entity == "user", AuditLog.entity_id == user_id)]
    if review_ids:
        conditions.append(and_(AuditLog.entity == "review", AuditLog.entity_id.in_(review_ids)))
    rows = list(db.scalars(
        select(AuditLog).where(or_(*conditions)).order_by(AuditLog.created_at.desc()).limit(100)
    ).all())
    actor_ids = {row.user_id for row in rows if row.user_id}
    actors = {u.id: u for u in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    return [{
        "id": row.id,
        "action": row.action,
        "details": row.details,
        "created_at": row.created_at,
        "actor_name": actors[row.user_id].full_name if row.user_id in actors else None,
        "actor_email": actors[row.user_id].email if row.user_id in actors else None,
        "actor_id": row.user_id,
        **history_media(db, row),
    } for row in rows]


@router.get("/users/{user_id}/reviews")
def user_reviews(user_id: str, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER, Role.MODERATOR))):
    rows = db.scalars(select(Review).join(AccessSession, Review.session_id == AccessSession.id).where(AccessSession.user_id == user_id).order_by(Review.created_at.desc())).all()
    return [{
        "id": row.id,
        "status": row.status,
        "comment": row.comment,
        "photo_url": photo_url(row.photo_key),
        "annotations": json.loads(row.annotations or "[]"),
        "created_at": row.created_at,
    } for row in rows]


@router.patch("/users/{user_id}")
def update_user(user_id: str, data: UserAdminUpdate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    confirm_password(actor, data.current_password)
    if data.role is not None:
        raise HTTPException(403, "Roles can only be changed from the web console")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == actor.id and data.is_blocked is True:
        raise HTTPException(409, "Administrator cannot block their own account")
    previous_role = user.role
    previous_blocked = user.is_blocked
    if data.is_blocked is not None:
        user.is_blocked = data.is_blocked
        user.ban_reason = data.ban_reason if data.is_blocked else None
        user.ban_until = datetime.fromisoformat(data.ban_until) if data.is_blocked and data.ban_until else None
    if data.warning_message is not None:
        user.warning_message = data.warning_message.strip() or None
    if user.role != previous_role:
        audit(db, actor, "user", user.id, "USER_ROLE_CHANGED", {"from": previous_role.value, "to": user.role.value})
    if user.is_blocked != previous_blocked:
        user.token_version += 1
        audit(db, actor, "user", user.id, "USER_BLOCKED" if user.is_blocked else "USER_UNBLOCKED", {"reason": user.ban_reason})
    db.commit()
    return {"id": user.id, "role": user.role, "is_blocked": user.is_blocked, "ban_reason": user.ban_reason, "ban_until": user.ban_until, "warning_message": user.warning_message}


@router.post("/users/{user_id}/score")
def adjust_score(user_id: str, data: ScoreAdjustment, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    confirm_password(actor, data.current_password)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    score = locked_score(db, user_id)
    score.points += data.amount
    db.flush()
    db.add(ScoreTransaction(user_id=user_id, amount=data.amount, balance_after=score.points, reason=data.reason, actor_id=actor.id))
    audit(db, actor, "user", user_id, "SCORE_ADJUSTED", {"amount": data.amount, "reason": data.reason, "balance": score.points})
    db.commit()
    return {"points": score.points}


@router.get("/users/{user_id}/score-transactions")
def score_transactions(user_id: str, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    rows = db.scalars(select(ScoreTransaction).where(ScoreTransaction.user_id == user_id).order_by(ScoreTransaction.created_at.desc()).limit(200)).all()
    return [{"id": row.id, "amount": row.amount, "balance_after": row.balance_after, "reason": row.reason, "actor_id": row.actor_id, "created_at": row.created_at} for row in rows]


@router.post("/users/{user_id}/revoke-sessions")
def revoke_user_sessions(user_id: str, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
    user.token_version += 1
    audit(db, actor, "user", user_id, "ALL_SESSIONS_REVOKED", {"count": len(rows)})
    db.commit()
    return {"revoked": len(rows)}


@router.get("/incidents")
def incidents(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    rows = db.scalars(select(Incident).order_by(Incident.created_at.desc()).offset(offset).limit(limit)).all()
    return [{"id": row.id, "composter_id": row.composter_id, "kind": row.kind, "severity": row.severity, "status": row.status, "title": row.title, "description": row.description, "photo_url": photo_url(row.photo_key) if row.photo_key else None, "assigned_to": row.assigned_to, "due_at": row.due_at, "created_at": row.created_at} for row in rows]


@router.post("/incidents")
def create_incident(data: IncidentCreate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    composter = db.get(Composter, data.composter_id) if data.composter_id else None
    if data.composter_id and not composter:
        raise HTTPException(404, "Composter not found")
    values = data.model_dump(exclude={"due_at"})
    values["title"] = data.title.strip() or incident_title(data.kind)
    row = Incident(**values, due_at=datetime.fromisoformat(data.due_at) if data.due_at else None, created_by=actor.id)
    db.add(row)
    db.flush()
    if row.kind == "OVERFLOW" and composter:
        composter.needs_emptying = True
    audit(db, actor, "incident", row.id, "INCIDENT_CREATED", {"severity": row.severity, "kind": row.kind})
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/incidents/report")
def report_incident(
    composter_id: str = Form(""),
    kind: str = Form("CONTAMINATION"),
    severity: str = Form("MEDIUM"),
    title: str = Form(""),
    description: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER)),
):
    composter = db.get(Composter, composter_id) if composter_id else None
    if composter_id and not composter:
        raise HTTPException(404, "Composter not found")
    kind = kind.strip().upper()
    severity = severity.strip().upper()
    title = title.strip() or incident_title(kind)
    description = description.strip()
    if len(kind) < 2 or len(kind) > 80 or severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        raise HTTPException(422, "Invalid incident fields")
    if len(title) < 3 or len(title) > 255 or len(description) > 5000:
        raise HTTPException(422, "Invalid incident fields")
    photo_key = None
    if file and file.filename:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(415, "Only images are accepted")
        photo_key = store_photo(file)
    row = Incident(
        composter_id=composter.id if composter else None,
        kind=kind,
        severity=severity,
        title=title,
        description=description,
        photo_key=photo_key,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    if row.kind == "OVERFLOW" and composter:
        composter.needs_emptying = True
    audit(db, actor, "incident", row.id, "INCIDENT_CREATED", {
        "composter_id": row.composter_id,
        "kind": row.kind,
        "severity": row.severity,
        "photo_key": photo_key,
    })
    if composter:
        audit(db, actor, "composter", composter.id, "INCIDENT_CREATED", {"incident_id": row.id, "kind": row.kind})
    db.commit()
    return {"id": row.id, "status": row.status, "photo_url": photo_url(photo_key) if photo_key else None}


@router.post("/composters/{composter_id}/contamination-report")
def contamination_report(
    composter_id: str,
    comment: str = Form("Выявлено загрязнение"),
    file: UploadFile = File(),
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER)),
):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(415, "Only images are accepted")
    photo_key = store_photo(file)
    description = comment.strip() or "Выявлено загрязнение"
    row = Incident(
        composter_id=composter.id,
        kind="CONTAMINATION",
        severity="MEDIUM",
        title="Выявлено загрязнение",
        description=description,
        photo_key=photo_key,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    audit(db, actor, "incident", row.id, "CONTAMINATION_REPORTED", {"composter_id": composter.id, "photo_key": photo_key})
    audit(db, actor, "composter", composter.id, "CONTAMINATION_REPORTED", {"incident_id": row.id})
    db.commit()
    return {"id": row.id, "status": row.status, "photo_url": photo_url(photo_key)}


@router.patch("/incidents/{incident_id}")
def update_incident(incident_id: str, data: IncidentUpdate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    row = db.get(Incident, incident_id)
    if not row:
        raise HTTPException(404, "Incident not found")
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    row.resolved_at = datetime.now(timezone.utc) if row.status == "RESOLVED" else None
    audit(db, actor, "incident", row.id, "INCIDENT_UPDATED", data.model_dump(exclude_none=True))
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/composters/{composter_id}/maintenance-mode")
def maintenance_mode(composter_id: str, data: MaintenanceModeRequest, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.maintenance_mode = data.enabled
    if data.enabled:
        composter.is_available = False
    audit(db, actor, "composter", composter.id, "MAINTENANCE_MODE_CHANGED", {"enabled": data.enabled})
    db.commit()
    return {"maintenance_mode": composter.maintenance_mode}


@router.get("/composters/{composter_id}/maintenance")
def maintenance_history(composter_id: str, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    rows = db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.composter_id == composter_id).order_by(MaintenanceRecord.created_at.desc())).all()
    return [{"id": row.id, "action": row.action, "notes": row.notes, "photo_key": row.photo_key, "engineer_id": row.engineer_id, "created_at": row.created_at} for row in rows]


@router.post("/composters/{composter_id}/maintenance")
def create_maintenance(composter_id: str, data: MaintenanceCreate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    if not db.get(Composter, composter_id):
        raise HTTPException(404, "Composter not found")
    row = MaintenanceRecord(composter_id=composter_id, engineer_id=actor.id, **data.model_dump())
    db.add(row)
    db.flush()
    audit(db, actor, "composter", composter_id, "MAINTENANCE_RECORDED", {"action": data.action})
    db.commit()
    return {"id": row.id}


@router.get("/analytics")
def analytics(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    reviewed = db.scalar(select(func.count(Review.id)).where(Review.status != ReviewStatus.PENDING)) or 0
    approved = db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.APPROVED)) or 0
    avg_seconds = db.scalar(select(func.avg(func.extract("epoch", Review.reviewed_at - Review.created_at))).where(Review.reviewed_at.is_not(None))) if db.bind.dialect.name != "sqlite" else None
    return {"reviewed": reviewed, "approved": approved, "approval_rate": round(approved / reviewed * 100, 1) if reviewed else 0, "average_review_seconds": float(avg_seconds) if avg_seconds else None, "open_incidents": db.scalar(select(func.count(Incident.id)).where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))) or 0, "storage_samples": db.scalar(select(func.count(Review.id))) or 0}


@router.post("/ml/datasets")
def freeze_dataset(db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    row = freeze_dataset_version(db, actor.id)
    return {"id": row.id, "version": row.version, "sample_count": row.sample_count, "annotated_count": row.annotated_count}


@router.get("/ml/datasets")
def dataset_versions(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    rows = db.scalars(select(DatasetVersion).order_by(DatasetVersion.version.desc())).all()
    return [{"id": row.id, "version": row.version, "status": row.status, "sample_count": row.sample_count, "annotated_count": row.annotated_count, "created_at": row.created_at} for row in rows]


@router.get("/ml/datasets/{version}")
def dataset_manifest(version: int, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    row = db.scalar(select(DatasetVersion).where(DatasetVersion.version == version))
    if not row:
        raise HTTPException(404, "Dataset version not found")
    return json.loads(row.manifest)


@router.get("/composters/{composter_id}/history")
def composter_history(composter_id: str, db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    if not db.get(Composter, composter_id):
        raise HTTPException(404, "Composter not found")
    rows = list(db.scalars(
        select(AuditLog).where(AuditLog.entity == "composter", AuditLog.entity_id == composter_id).order_by(AuditLog.created_at.desc()).limit(100)
    ).all())
    actor_ids = {row.user_id for row in rows if row.user_id}
    actors = {u.id: u for u in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    return [{
        "id": row.id,
        "action": row.action,
        "details": row.details,
        "created_at": row.created_at,
        "actor_name": actors[row.user_id].full_name if row.user_id in actors else None,
        "actor_email": actors[row.user_id].email if row.user_id in actors else None,
        "actor_id": row.user_id,
        **history_media(db, row),
    } for row in rows]


@router.get("/composters")
def composters(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    query = select(Composter).order_by(Composter.name).offset(offset).limit(limit)
    return [{"id": c.id, "name": c.name, "device_id": c.device_id, "latitude": c.latitude, "longitude": c.longitude, "radius_m": c.radius_m, "is_available": c.is_available, "needs_emptying": c.needs_emptying, "battery_level": c.battery_level, "lock_state": c.lock_state, "last_seen_at": c.last_seen_at, "qr_payload": f"compost://composter/{c.id}"} for c in db.scalars(query).all()]


@router.post("/composters")
def create_composter(data: ComposterCreate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    existing = db.scalar(select(Composter).where(Composter.device_id == data.device_id))
    if existing:
        existing.name = data.name
        existing.latitude = data.latitude
        existing.longitude = data.longitude
        existing.radius_m = data.radius_m
        audit(db, actor, "composter", existing.id, "PROVISIONING_RESUMED")
        db.commit()
        return {"id": existing.id, "qr_payload": f"compost://composter/{existing.id}", "device_secret": existing.secret}
    device_secret = data.secret or secrets.token_hex(32)
    composter = Composter(**data.model_dump(exclude={"secret"}), secret=device_secret)
    db.add(composter)
    db.flush()
    audit(db, actor, "composter", composter.id, "COMPOSTER_CREATED")
    db.commit()
    return {"id": composter.id, "qr_payload": f"compost://composter/{composter.id}", "device_secret": device_secret}


@router.patch("/composters/{composter_id}")
def update_composter(composter_id: str, data: ComposterUpdate, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    if not data.current_password:
        raise HTTPException(422, "Password confirmation required")
    confirm_password(actor, data.current_password)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    for key, value in data.model_dump(exclude_none=True, exclude={"current_password"}).items():
        setattr(composter, key, value)
    audit(db, actor, "composter", composter.id, "COMPOSTER_UPDATED", data.model_dump(exclude_none=True))
    db.commit()
    return {"id": composter.id, "is_available": composter.is_available}


@router.post("/composters/{composter_id}/activate-provisioned")
def activate_provisioned(composter_id: str, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.ENGINEER))):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.is_available = True
    audit(db, actor, "composter", composter.id, "PROVISIONING_COMPLETED")
    db.commit()
    return {"status": "active"}


@router.post("/composters/{composter_id}/clear-full")
def clear_full_report(composter_id: str, data: PasswordConfirmation, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    confirm_password(actor, data.current_password)
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        raise HTTPException(404, "Composter not found")
    set_full_state_from_incident(db, actor, composter, False)
    db.commit()
    return {"status": "cleared"}


@router.post("/composters/{composter_id}/full-state")
def set_full_state(composter_id: str, data: FullStateRequest, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN, Role.MODERATOR, Role.ENGINEER))):
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        raise HTTPException(404, "Composter not found")
    set_full_state_from_incident(db, actor, composter, data.needs_emptying)
    db.commit()
    return {"status": "full" if data.needs_emptying else "cleared"}


@router.post("/composters/{composter_id}/delete")
def delete_composter(composter_id: str, data: PasswordConfirmation, db: Session = Depends(get_db), actor: User = Depends(require_roles(Role.ADMIN))):
    confirm_password(actor, data.current_password)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    session_ids = list(db.scalars(select(AccessSession.id).where(AccessSession.composter_id == composter.id)).all())
    review_ids = list(db.scalars(select(Review.id).where(Review.session_id.in_(session_ids))).all()) if session_ids else []
    if review_ids:
        # Keep points history even when its source review is removed.
        db.execute(update(ScoreTransaction).where(ScoreTransaction.review_id.in_(review_ids)).values(review_id=None))
        db.execute(delete(Violation).where(Violation.review_id.in_(review_ids)))
        db.execute(delete(Review).where(Review.id.in_(review_ids)))
    if session_ids:
        db.execute(delete(DeviceCommand).where(DeviceCommand.session_id.in_(session_ids)))
        db.execute(delete(AccessSession).where(AccessSession.id.in_(session_ids)))
    db.execute(delete(Telemetry).where(Telemetry.composter_id == composter.id))
    db.add(OutboxEvent(destination="telemetry", kind="composter.deleted", payload=json.dumps({"composter_id": composter.id})))
    db.execute(delete(ProximityChallenge).where(ProximityChallenge.composter_id == composter.id))
    db.execute(delete(MaintenanceRecord).where(MaintenanceRecord.composter_id == composter.id))
    # Incident history remains accessible without a live device reference.
    db.execute(update(Incident).where(Incident.composter_id == composter.id).values(composter_id=None))
    db.delete(composter)
    audit(db, actor, "composter", composter_id, "COMPOSTER_DELETED")
    db.commit()
    return {"status": "deleted"}


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
    db.add(OutboxEvent(destination="telemetry", kind="telemetry.recorded", payload=json.dumps({
        "composter_id": composter.id,
        **data.model_dump(),
        "reported_at": composter.last_seen_at.isoformat(),
    })))
    audit(db, actor, "composter", composter.id, "TELEMETRY_RECORDED", data.model_dump())
    db.commit()
    return {"status": "recorded"}


@router.get("/audit")
def audit_log(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.ADMIN))):
    return [{"id": row.id, "entity": row.entity, "entity_id": row.entity_id, "action": row.action, "user_id": row.user_id, "details": row.details, "created_at": row.created_at} for row in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500)).all()]
