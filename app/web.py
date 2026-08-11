import io
import json
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import boto3
import httpx
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from .config import get_settings
from .domain.incidents import audit_incident_resolved, set_full_state_from_incident, sync_full_state_after_incident_change
from .domain.storage import validated_image
from .ml_dataset import build_dataset_manifest, freeze_dataset_version, training_readiness
from .models import AccessSession, AuditLog, Composter, DatasetVersion, DeviceCommand, Incident, MaintenanceRecord, MLDatasetPhotoArchive, MLDatasetSample, MLDatasetSampleArchive, ModelTrainingRun, Review, ReviewStatus, Role, ScoreTransaction, StorageObjectAnnotation, User, UserScore, Violation
from .security import create_token, verify_password, web_current_user
from .services import apply_violation, audit, create_moderation_incident, incident_title, photo_url, reward_review, store_photo
from .web_support.security import csrf, generate_csrf, require_role
from .web_support.storage import firmware_rows, firmware_s3, human_size, normalized_annotations, storage_file_label
from .web_support.context import page_context
from .web_support.history import history_presentation

router = APIRouter(prefix="/web", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def history_rows(db: Session, conditions) -> list[dict]:
    rows = list(db.scalars(select(AuditLog).where(or_(*conditions)).order_by(AuditLog.created_at.desc()).limit(100)).all())
    actor_ids = {row.user_id for row in rows if row.user_id}
    actors = {user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    result = []
    for row in rows:
        media = {"photo_url": None, "annotations": [], "composter": None}
        if row.entity == "review":
            review = db.get(Review, row.entity_id)
            if review:
                session = db.get(AccessSession, review.session_id)
                media = {
                    "photo_url": photo_url(review.photo_key),
                    "annotations": json.loads(review.annotations or "[]"),
                    "composter": db.get(Composter, session.composter_id) if session else None,
                }
        elif row.entity == "composter" and row.action == "CONTAMINATION_REPORTED":
            details = json.loads(row.details or "{}")
            incident = db.get(Incident, details.get("incident_id")) if details.get("incident_id") else None
            if incident and incident.photo_key:
                media = {"photo_url": photo_url(incident.photo_key), "annotations": [], "composter": db.get(Composter, row.entity_id)}
        title, tag, tag_class = history_presentation(row.action)
        result.append({"row": row, "action": row.action, "actor": actors.get(row.user_id), "title": title, "tag": tag, "tag_class": tag_class, "created_at": row.created_at, "detail_url": f"/web/history/{row.id}", **media})
    return result


def review_queue(db: Session) -> list[dict]:
    result = []
    for review in db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all():
        session = db.get(AccessSession, review.session_id)
        result.append({
            "review": review,
            "session": session,
            "photo_url": photo_url(review.photo_key),
            "violations": ", ".join(json.loads(review.ml_violations)),
            "annotations": review.annotations or "[]",
        })
    return result


def recent_review_history(db: Session) -> list[dict]:
    result = []
    rows = db.scalars(
        select(Review)
        .where(Review.status != ReviewStatus.PENDING)
        .order_by(func.coalesce(Review.reviewed_at, Review.created_at).desc())
        .limit(50)
    ).all()
    for review in rows:
        session = db.get(AccessSession, review.session_id)
        if not session:
            continue
        result.append({
            "review": review,
            "session": session,
            "user": db.get(User, session.user_id),
            "composter": db.get(Composter, session.composter_id),
            "reviewer": db.get(User, review.reviewed_by) if review.reviewed_by else None,
            "photo_url": photo_url(review.photo_key),
            "violation": db.scalar(select(Violation).where(Violation.review_id == review.id)),
            "detail_url": f"/web/deposits/{session.id}",
        })
    return result


@router.get("")
def web_root(request: Request):
    return RedirectResponse("/web/dashboard" if request.cookies.get("compost_session") else "/web/login", 303)


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login_submit(request: Request, email: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.lower(), User.deleted_at.is_(None)))
    if not user or not verify_password(password, user.password_hash) or user.role == Role.USER:
        return templates.TemplateResponse(request, "login.html", {"error": "Неверные данные или у аккаунта нет доступа к панели"}, status_code=401)
    response = RedirectResponse("/web/dashboard", 303)
    cookie_secure = get_settings().cookie_secure
    response.set_cookie("compost_session", create_token(user), httponly=True, secure=cookie_secure, samesite="strict", max_age=3600)
    response.set_cookie("compost_csrf", generate_csrf(), httponly=True, secure=cookie_secure, samesite="strict", max_age=3600)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form()):
    csrf(request, csrf_token)
    response = RedirectResponse("/web/login", 303)
    response.delete_cookie("compost_session")
    response.delete_cookie("compost_csrf")
    return response


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    return templates.TemplateResponse(request, "dashboard.html", page_context(request, actor, db, "dashboard"))


@router.get("/equipment")
def equipment_page(request: Request, q: str = Query(""), status: str = Query("ALL"), page: int = Query(1, ge=1), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    query = select(Composter)
    needle = q.strip().lower()
    if needle:
        query = query.where(or_(func.lower(Composter.name).contains(needle), func.lower(Composter.device_id).contains(needle)))
    if status == "ACTIVE":
        query = query.where(Composter.is_available.is_(True))
    elif status == "DISABLED":
        query = query.where(Composter.is_available.is_(False))
    elif status == "FULL":
        query = query.where(Composter.needs_emptying.is_(True))
    elif status == "LOW_BATTERY":
        query = query.where(Composter.battery_level <= 20)
    rows = list(db.scalars(query.order_by(Composter.name).offset((page - 1) * 50).limit(51)).all())
    return templates.TemplateResponse(request, "equipment.html", page_context(request, actor, db, "equipment", composters=rows[:50], q=q, status=status, page=page, has_next=len(rows) > 50))


@router.get("/equipment/{composter_id}")
def equipment_detail(request: Request, composter_id: str, error: str = Query(""), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    history = history_rows(db, [and_(AuditLog.entity == "composter", AuditLog.entity_id == composter.id)])
    return templates.TemplateResponse(request, "equipment_detail.html", page_context(request, actor, db, "equipment", composter=composter, history=history, error=error))


@router.post("/equipment/{composter_id}")
def change_composter(
    request: Request,
    composter_id: str,
    name: str = Form(),
    latitude: float = Form(),
    longitude: float = Form(),
    available: bool = Form(False),
    current_password: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    if not verify_password(current_password, actor.password_hash):
        return RedirectResponse(f"/web/equipment/{composter_id}?error=Неверный+пароль", 303)
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    changes = {"name": name.strip(), "latitude": latitude, "longitude": longitude, "available": available}
    composter.name = changes["name"] or composter.name
    composter.latitude = latitude
    composter.longitude = longitude
    composter.is_available = available
    audit(db, actor, "composter", composter.id, "COMPOSTER_UPDATED", changes)
    db.commit()
    return RedirectResponse(f"/web/equipment/{composter.id}", 303)


@router.post("/equipment/{composter_id}/full-state")
def change_full_state(request: Request, composter_id: str, needs_emptying: bool = Form(), from_moderation: bool = Form(False), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    composter = db.scalar(select(Composter).where(Composter.id == composter_id).with_for_update())
    if not composter:
        raise HTTPException(404, "Composter not found")
    set_full_state_from_incident(db, actor, composter, needs_emptying)
    db.commit()
    return RedirectResponse("/web/moderation" if from_moderation else f"/web/equipment/{composter.id}", 303)


@router.get("/users")
def users_page(request: Request, q: str = Query(""), status: str = Query("ALL"), page: int = Query(1, ge=1), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    query = select(User).where(User.deleted_at.is_(None))
    needle = q.strip().lower()
    if needle:
        query = query.where(or_(func.lower(User.email).contains(needle), func.lower(User.full_name).contains(needle)))
    if status == "ACTIVE":
        query = query.where(User.is_blocked.is_(False))
    elif status == "BLOCKED":
        query = query.where(User.is_blocked.is_(True))
    elif status == "STAFF":
        query = query.where(User.role != Role.USER)
    rows = list(db.scalars(query.order_by(User.created_at.desc()).offset((page - 1) * 50).limit(51)).all())
    return templates.TemplateResponse(request, "users.html", page_context(request, actor, db, "users", users=rows[:50], q=q, status=status, page=page, has_next=len(rows) > 50))


@router.get("/users/{user_id}")
def user_detail(request: Request, user_id: str, error: str = Query(""), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    reviews = list(db.scalars(select(Review).join(AccessSession, Review.session_id == AccessSession.id).where(AccessSession.user_id == user.id).order_by(Review.created_at.desc())).all())
    review_ids = [review.id for review in reviews]
    review_rows = [{
        "review": review,
        "session": db.get(AccessSession, review.session_id),
        "photo_url": photo_url(review.photo_key),
        "annotations": json.loads(review.annotations or "[]"),
    } for review in reviews]
    conditions = [
        and_(AuditLog.entity == "user", AuditLog.entity_id == user.id),
        AuditLog.user_id == user.id,
    ]
    if review_ids:
        conditions.append(and_(AuditLog.entity == "review", AuditLog.entity_id.in_(review_ids)))
    score = db.get(UserScore, user.id)
    transactions = list(db.scalars(select(ScoreTransaction).where(ScoreTransaction.user_id == user.id).order_by(ScoreTransaction.created_at.desc())).all())
    history = history_rows(db, conditions)
    review_map = {review.id: review for review in reviews}
    transaction_review_ids = {item.review_id for item in transactions if item.review_id}
    legacy_reviews = [review for review in reviews if review.status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED) and review.id not in transaction_review_ids]
    score_actor_ids = {item.actor_id for item in transactions if item.actor_id} | {review.reviewed_by for review in legacy_reviews if review.reviewed_by}
    score_actors = {item.id: item for item in db.scalars(select(User).where(User.id.in_(score_actor_ids))).all()} if score_actor_ids else {}
    review_audits = {
        item["row"].entity_id: item
        for item in history
        if item["row"].entity == "review" and item["action"] in ("REVIEW_APPROVED", "REVIEW_REJECTED")
    }
    score_events = [(item.created_at, "transaction", item) for item in transactions]
    score_events.extend((review.reviewed_at or review.created_at, "legacy", review) for review in legacy_reviews)
    running_balance = 0
    for _, event_kind, event in sorted(score_events, key=lambda row: row[0]):
        if event_kind == "transaction":
            item = event
            running_balance = item.balance_after
            amount = item.amount
            review = review_map.get(item.review_id)
            event_actor = score_actors.get(item.actor_id)
            title = item.reason
            detail_url = f"/web/score-transactions/{item.id}"
        else:
            review = event
            previous_balance = running_balance
            running_balance = previous_balance + 10 if review.status == ReviewStatus.APPROVED else max(0, previous_balance - 15)
            amount = running_balance - previous_balance
            audit_item = review_audits.get(review.id)
            item = {
                "id": f"legacy-{review.id}",
                "amount": amount,
                "balance_after": running_balance,
            }
            event_actor = audit_item["actor"] if audit_item else score_actors.get(review.reviewed_by)
            title = "Загрузка одобрена" if review.status == ReviewStatus.APPROVED else "Загрузка отклонена"
            detail_url = audit_item["detail_url"] if audit_item else f"/web/deposits/{review.session_id}"
        session = db.get(AccessSession, review.session_id) if review else None
        composter = db.get(Composter, session.composter_id) if session else None
        history.append({
            "row": None,
            "action": "SCORE_ADJUSTED",
            "category": "POINTS",
            "score": item,
            "actor": event_actor,
            "title": title,
            "tag": "Баллы",
            "tag_class": "score-positive" if amount > 0 else "score-negative",
            "created_at": review_audits.get(review.id, {}).get("created_at", review.reviewed_at or review.created_at) if event_kind == "legacy" else item.created_at,
            "photo_url": photo_url(review.photo_key) if review else None,
            "annotations": json.loads(review.annotations or "[]") if review else [],
            "composter": composter,
            "detail_url": detail_url,
        })
    history.sort(key=lambda item: item["created_at"], reverse=True)
    return templates.TemplateResponse(request, "user_detail.html", page_context(request, actor, db, "users", user=user, history=history, reviews=review_rows, roles=list(Role), score=score, error=error))


@router.post("/users/{user_id}")
def change_user(request: Request, user_id: str, role: str = Form(), blocked: bool = Form(False), ban_reason: str = Form(""), ban_until: str = Form(""), current_password: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    if not verify_password(current_password, actor.password_hash):
        return RedirectResponse(f"/web/users/{user_id}?error=Неверный+пароль", 303)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    requested_role = Role(role)
    if user.role == Role.ADMIN and requested_role != Role.ADMIN:
        admin_count = db.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN)) or 0
        if admin_count <= 1:
            raise HTTPException(409, "Нельзя снять роль у последнего администратора")
    previous_role, previous_blocked = user.role, user.is_blocked
    user.role = requested_role
    user.is_blocked = blocked
    user.ban_reason = ban_reason.strip() if blocked else None
    user.ban_until = datetime.fromisoformat(ban_until) if blocked and ban_until else None
    if user.role != previous_role or user.is_blocked != previous_blocked:
        user.token_version += 1
    if user.role != previous_role:
        audit(db, actor, "user", user.id, "USER_ROLE_CHANGED", {"from": previous_role.value, "to": user.role.value})
    if user.is_blocked != previous_blocked:
        audit(db, actor, "user", user.id, "USER_BLOCKED" if user.is_blocked else "USER_UNBLOCKED", {"reason": user.ban_reason})
    db.commit()
    return RedirectResponse(f"/web/users/{user.id}", 303)


@router.post("/users/{user_id}/score")
def change_user_score(request: Request, user_id: str, amount: int = Form(), reason: str = Form(), current_password: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    if not verify_password(current_password, actor.password_hash):
        return RedirectResponse(f"/web/users/{user_id}?error=Неверный+пароль", 303)
    if not -10_000 <= amount <= 10_000 or len(reason.strip()) < 3:
        raise HTTPException(422, "Некорректная корректировка")
    score = db.get(UserScore, user_id)
    if not score:
        score = UserScore(user_id=user_id, points=0)
        db.add(score)
    score.points += amount
    db.flush()
    db.add(ScoreTransaction(user_id=user_id, amount=amount, balance_after=score.points, reason=reason.strip(), actor_id=actor.id))
    audit(db, actor, "user", user_id, "SCORE_ADJUSTED", {"amount": amount, "reason": reason.strip(), "balance": score.points})
    db.commit()
    return RedirectResponse(f"/web/users/{user_id}", 303)


@router.get("/deposits/{session_id}")
def deposit_detail(request: Request, session_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    session = db.get(AccessSession, session_id)
    if not session:
        raise HTTPException(404, "Deposit not found")
    review = db.scalar(select(Review).where(Review.session_id == session.id))
    violation = db.scalar(select(Violation).where(Violation.review_id == review.id)) if review else None
    transactions = list(db.scalars(select(ScoreTransaction).where(ScoreTransaction.review_id == review.id).order_by(ScoreTransaction.created_at.desc())).all()) if review else []
    commands = list(db.scalars(select(DeviceCommand).where(DeviceCommand.session_id == session.id).order_by(DeviceCommand.issued_at)).all())
    entity_ids = [session.id]
    if review:
        entity_ids.append(review.id)
    if violation:
        entity_ids.append(violation.id)
    audits = list(db.scalars(select(AuditLog).where(AuditLog.entity_id.in_(entity_ids)).order_by(AuditLog.created_at.desc())).all())
    actor_ids = {row.user_id for row in audits if row.user_id} | {row.actor_id for row in transactions if row.actor_id}
    actors = {row.id: row for row in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    return templates.TemplateResponse(
        request,
        "deposit_detail.html",
        page_context(
            request,
            actor,
            db,
            "users",
            session=session,
            user=db.get(User, session.user_id),
            composter=db.get(Composter, session.composter_id),
            review=review,
            violation=violation,
            transactions=transactions,
            commands=commands,
            audits=audits,
            actors=actors,
            photo_url_value=photo_url(review.photo_key) if review else None,
            annotations=json.loads(review.annotations or "[]") if review else [],
        ),
    )


@router.get("/history/{audit_id}")
def history_detail(request: Request, audit_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    row = db.get(AuditLog, audit_id)
    if not row:
        raise HTTPException(404, "History entry not found")
    try:
        details = json.loads(row.details or "{}")
    except json.JSONDecodeError:
        details = {"raw": row.details}
    related_url = None
    if row.entity == "review":
        review = db.get(Review, row.entity_id)
        related_url = f"/web/deposits/{review.session_id}" if review else None
    elif row.entity in {"violation", "incident"}:
        related_url = f"/web/violations/{row.entity_id}"
    elif row.entity == "composter":
        related_url = f"/web/equipment/{row.entity_id}"
    elif row.entity == "user":
        related_url = f"/web/users/{row.entity_id}"
    media = history_rows(db, [AuditLog.id == row.id])
    return templates.TemplateResponse(
        request,
        "history_detail.html",
        page_context(
            request,
            actor,
            db,
            "dashboard",
            entry=row,
            details=details,
            entry_actor=db.get(User, row.user_id) if row.user_id else None,
            related_url=related_url,
            media=media[0] if media else None,
            score=None,
        ),
    )


@router.get("/score-transactions/{transaction_id}")
def score_transaction_detail(request: Request, transaction_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    transaction = db.get(ScoreTransaction, transaction_id)
    if not transaction:
        raise HTTPException(404, "Score transaction not found")
    review = db.get(Review, transaction.review_id) if transaction.review_id else None
    session = db.get(AccessSession, review.session_id) if review else None
    return templates.TemplateResponse(
        request,
        "history_detail.html",
        page_context(
            request,
            actor,
            db,
            "users",
            entry=None,
            details={},
            entry_actor=db.get(User, transaction.actor_id) if transaction.actor_id else None,
            related_url=f"/web/deposits/{session.id}" if session else f"/web/users/{transaction.user_id}",
            media={"photo_url": photo_url(review.photo_key), "annotations": json.loads(review.annotations or "[]")} if review else None,
            score=transaction,
        ),
    )


@router.get("/moderation")
def moderation_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.MODERATOR)
    return templates.TemplateResponse(request, "moderation.html", page_context(request, actor, db, "moderation", reviews=review_queue(db), review_history=recent_review_history(db)))


@router.get("/storage")
def storage_page(request: Request, q: str = Query(""), cursor: str | None = Query(None, max_length=1024), page: int = Query(1, ge=1), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    settings = get_settings()
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    try:
        result = client.list_objects_v2(
            Bucket=settings.s3_bucket,
            Prefix=q.strip(),
            MaxKeys=100,
            **({"ContinuationToken": cursor} if cursor else {}),
        )
        objects = result.get("Contents", [])
        next_cursor = result.get("NextContinuationToken")
        files = [
            {
                "key": item["Key"],
                "size": item["Size"],
                "modified": item["LastModified"],
                "url": photo_url(item["Key"]),
                "detail_url": f"/web/storage/object/{quote(item['Key'], safe='/')}",
                "label": storage_file_label(item["Key"]),
                "short_id": Path(item["Key"]).name.split("-", 1)[0],
                "size_label": human_size(item["Size"]),
            }
            for item in objects
            if item["Size"] >= 128
        ]
        files.sort(key=lambda item: item["modified"], reverse=True)
        storage_error = None
    except Exception as exc:
        files, storage_error, next_cursor = [], str(exc), None
    return templates.TemplateResponse(request, "storage.html", page_context(request, actor, db, "storage", files=files, q=q, storage_error=storage_error, next_cursor=next_cursor, page=page))


@router.get("/storage/object/{object_key:path}")
def storage_object_detail(request: Request, object_key: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    settings = get_settings()
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    try:
        head = client.head_object(Bucket=settings.s3_bucket, Key=object_key)
    except Exception as exc:
        raise HTTPException(404, "Object not found") from exc
    reviews = list(db.scalars(select(Review).where(Review.photo_key == object_key).order_by(Review.created_at.desc())).all())
    incidents = list(db.scalars(select(Incident).where(Incident.photo_key == object_key).order_by(Incident.created_at.desc())).all())
    maintenance = list(db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.photo_key == object_key).order_by(MaintenanceRecord.created_at.desc())).all())
    references = []
    for review in reviews:
        session = db.get(AccessSession, review.session_id)
        violation = db.scalar(select(Violation).where(Violation.review_id == review.id))
        references.append({
            "type": "Проверка закладки",
            "title": review.status.value,
            "url": f"/web/deposits/{session.id}" if session else None,
            "meta": f"Review {review.id}",
        })
        if violation:
            references.append({"type": "Нарушение", "title": violation.reason, "url": f"/web/violations/{violation.id}", "meta": violation.id})
    for incident in incidents:
        references.append({"type": "Инцидент", "title": incident.title, "url": f"/web/violations/{incident.id}", "meta": incident.id})
    for record in maintenance:
        references.append({"type": "Обслуживание", "title": record.action, "url": f"/web/equipment/{record.composter_id}", "meta": record.id})
    audits = list(db.scalars(select(AuditLog).where(AuditLog.details.contains(object_key)).order_by(AuditLog.created_at.desc()).limit(100)).all())
    stored_annotations = db.get(StorageObjectAnnotation, object_key)
    if stored_annotations:
        annotations = json.loads(stored_annotations.annotations or "[]")
    else:
        annotations = []
        for review in reviews:
            review_annotations = json.loads(review.annotations or "[]")
            if review_annotations:
                annotations = review_annotations
                break
    return templates.TemplateResponse(
        request,
        "storage_object_detail.html",
        page_context(
            request,
            actor,
            db,
            "storage",
            object_key=object_key,
            object_url=photo_url(object_key),
            object_name=Path(object_key).name,
            object_size=head.get("ContentLength", 0),
            object_type=head.get("ContentType", "application/octet-stream"),
            object_modified=head.get("LastModified"),
            object_etag=str(head.get("ETag", "")).strip('"'),
            object_metadata=head.get("Metadata", {}),
            references=references,
            audits=audits,
            annotations=annotations,
            annotation_record=stored_annotations,
            annotation_actor=db.get(User, stored_annotations.updated_by) if stored_annotations and stored_annotations.updated_by else None,
        ),
    )


@router.post("/storage/annotations/{object_key:path}")
def update_storage_annotations(
    request: Request,
    object_key: str,
    annotations: str = Form("[]"),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    settings = get_settings()
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=object_key)
    except Exception as exc:
        raise HTTPException(404, "Object not found") from exc
    boxes = normalized_annotations(annotations)
    encoded = json.dumps(boxes, ensure_ascii=False)
    row = db.get(StorageObjectAnnotation, object_key)
    if row:
        row.annotations = encoded
        row.updated_by = actor.id
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = StorageObjectAnnotation(object_key=object_key, annotations=encoded, updated_by=actor.id)
        db.add(row)
    reviews = list(db.scalars(select(Review).where(Review.photo_key == object_key)).all())
    for review in reviews:
        review.annotations = encoded
    audit(db, actor, "storage_object", object_key, "STORAGE_ANNOTATIONS_UPDATED", {
        "key": object_key,
        "boxes": len(boxes),
        "review_ids": [review.id for review in reviews],
    })
    db.commit()
    return RedirectResponse(f"/web/storage/object/{quote(object_key, safe='/')}", 303)


@router.post("/storage/upload")
def storage_upload(request: Request, file: UploadFile = File(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(415, "Можно загружать только изображения")
    settings = get_settings()
    payload, content_type = validated_image(file)
    safe_name = (file.filename or "photo.jpg").replace("/", "_").replace("\\", "_")
    key = f"engineering/{actor.id}/{secrets.token_hex(8)}-{safe_name}"
    client = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)
    client.upload_fileobj(io.BytesIO(payload), settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})
    audit(db, actor, "storage", actor.id, "ENGINEERING_PHOTO_UPLOADED", {"key": key})
    db.commit()
    return RedirectResponse("/web/storage?q=engineering/", 303)


@router.get("/ml")
def ml_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN)
    dataset = build_dataset_manifest(db)
    summary = dataset["summary"]
    versions = list(db.scalars(select(DatasetVersion).order_by(DatasetVersion.version.desc())).all())
    version_readiness = [
        {"version": row, "readiness": training_readiness(json.loads(row.manifest))}
        for row in versions
    ]
    trainable_versions = [item for item in version_readiness if item["readiness"]["trainable"]]
    latest_version_readiness = version_readiness[0]["readiness"] if version_readiness else None
    runs = list(db.scalars(select(ModelTrainingRun).order_by(ModelTrainingRun.created_at.desc()).limit(20)).all())
    deployed = db.scalar(
        select(ModelTrainingRun)
        .where(ModelTrainingRun.deployed_at.is_not(None))
        .order_by(ModelTrainingRun.deployed_at.desc())
    )
    try:
        service_status = httpx.get(f"{get_settings().ml_service_url}/health", timeout=0.35).json()
    except Exception:
        service_status = {"status": "offline", "model": "недоступен"}
    return templates.TemplateResponse(
        request,
        "ml.html",
        page_context(
            request,
            actor,
            db,
            "ml",
            reviewed=summary["samples"],
            annotated=summary["annotated"],
            approved=summary["labels"].get("clean", 0),
            contaminated=summary["labels"].get("contamination", 0),
            versions=versions,
            latest_version=versions[0] if versions else None,
            latest_version_readiness=latest_version_readiness,
            trainable_versions=trainable_versions,
            runs=runs,
            deployed=deployed,
            service_status=service_status,
        ),
    )


@router.get("/ml/dataset-create")
def create_dataset_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN)
    versions = list(db.scalars(select(DatasetVersion).order_by(DatasetVersion.version.desc())).all())
    sample_usage: dict[str, list[int]] = {}
    for version in versions:
        for version_sample in json.loads(version.manifest).get("samples", []):
            identifier = str(version_sample.get("review_id", ""))
            if identifier and not identifier.startswith(("manual:", "review:")):
                identifier = f"review:{identifier}"
            if identifier:
                sample_usage.setdefault(identifier, []).append(version.version)

    archived_ids = set(db.scalars(select(MLDatasetPhotoArchive.source_key)).all())
    archived_ids |= {f"manual:{value}" for value in db.scalars(select(MLDatasetSampleArchive.sample_id)).all()}
    rows = []
    for sample in db.scalars(select(MLDatasetSample).order_by(MLDatasetSample.created_at.desc())).all():
        source_key = f"manual:{sample.id}"
        rows.append({
            "id": source_key,
            "photo_url": photo_url(sample.photo_key),
            "label": sample.label,
            "annotations": normalized_annotations(sample.annotations),
            "source_type": "manual",
            "source": f"Ручная загрузка · {sample.source_group}",
            "created_at": sample.created_at,
            "used_versions": sample_usage.get(source_key, []),
            "archived": source_key in archived_ids,
        })

    reviewed = list(db.execute(
        select(Review, AccessSession)
        .join(AccessSession, Review.session_id == AccessSession.id)
        .where(Review.status != ReviewStatus.PENDING)
        .order_by(func.coalesce(Review.reviewed_at, Review.created_at).desc())
    ).all())
    composter_ids = {session.composter_id for _, session in reviewed}
    composters = {row.id: row for row in db.scalars(select(Composter).where(Composter.id.in_(composter_ids))).all()} if composter_ids else {}
    for review, session in reviewed:
        source_key = f"review:{review.id}"
        composter = composters.get(session.composter_id)
        rows.append({
            "id": source_key,
            "photo_url": photo_url(review.photo_key),
            "label": "CONTAMINATION" if review.status == ReviewStatus.REJECTED else "CLEAN",
            "annotations": normalized_annotations(review.annotations),
            "source_type": "moderated",
            "source": f"Модерация · {composter.name if composter else session.composter_id}",
            "created_at": review.reviewed_at or review.created_at,
            "used_versions": sample_usage.get(source_key, []),
            "archived": source_key in archived_ids,
        })
    rows.sort(key=lambda row: row["created_at"].isoformat() if row["created_at"] else "", reverse=True)
    return templates.TemplateResponse(
        request,
        "ml_dataset_create.html",
        page_context(
            request,
            actor,
            db,
            "ml",
            ml_samples=rows,
            unused_samples=sum(not row["archived"] and not row["used_versions"] for row in rows),
            used_samples=sum(not row["archived"] and bool(row["used_versions"]) for row in rows),
            archived_samples=sum(row["archived"] for row in rows),
            moderated_samples=sum(row["source_type"] == "moderated" and not row["archived"] for row in rows),
            annotated_samples=sum(bool(row["annotations"]) and not row["archived"] for row in rows),
            unannotated_samples=sum(not row["annotations"] and not row["archived"] for row in rows),
        ),
    )


@router.post("/ml/samples")
def upload_ml_sample_page(
    request: Request,
    file: UploadFile = File(),
    label: str = Form(),
    annotations: str = Form("[]"),
    source_group: str = Form("manual"),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    normalized_label = label.strip().upper()
    if normalized_label not in {"CLEAN", "CONTAMINATION"}:
        raise HTTPException(422, "Неизвестный класс ML-примера")
    boxes = normalized_annotations(annotations) if normalized_label == "CONTAMINATION" else []
    if normalized_label == "CONTAMINATION" and not boxes:
        raise HTTPException(422, "Для загрязнения отметьте хотя бы одну область")
    group = source_group.strip()[:120] or "manual"
    photo_key = store_photo(file)
    sample = MLDatasetSample(
        photo_key=photo_key,
        label=normalized_label,
        annotations=json.dumps(boxes, ensure_ascii=False),
        source_group=group,
        created_by=actor.id,
    )
    db.add(sample)
    db.flush()
    audit(db, actor, "ml_sample", sample.id, "ML_SAMPLE_UPLOADED", {"label": normalized_label, "boxes": len(boxes), "source_group": group})
    db.commit()
    return RedirectResponse("/web/ml/dataset-create", 303)


@router.post("/ml/samples/archive")
def archive_ml_samples_page(
    request: Request,
    sample_ids: list[str] = Form(default=[]),
    action: str = Form("archive"),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    identifiers = {value if ":" in value else f"manual:{value}" for value in sample_ids}
    if not identifiers:
        raise HTTPException(422, "Выберите хотя бы одно фото")
    manual_ids = {value.removeprefix("manual:") for value in identifiers if value.startswith("manual:")}
    review_ids = {value.removeprefix("review:") for value in identifiers if value.startswith("review:")}
    if len(manual_ids) + len(review_ids) != len(identifiers):
        raise HTTPException(422, "Неизвестный источник фотографии")
    samples = list(db.scalars(select(MLDatasetSample).where(MLDatasetSample.id.in_(manual_ids))).all()) if manual_ids else []
    reviews = list(db.scalars(select(Review).where(Review.id.in_(review_ids), Review.status != ReviewStatus.PENDING)).all()) if review_ids else []
    if len(samples) != len(manual_ids) or len(reviews) != len(review_ids):
        raise HTTPException(404, "Часть фотографий не найдена")
    archives = {row.source_key: row for row in db.scalars(select(MLDatasetPhotoArchive).where(MLDatasetPhotoArchive.source_key.in_(identifiers))).all()}
    if action == "archive":
        for identifier in identifiers:
            if identifier not in archives:
                db.add(MLDatasetPhotoArchive(source_key=identifier, archived_by=actor.id))
        audit(db, actor, "ml_sample", actor.id, "ML_SAMPLES_ARCHIVED", {"source_keys": sorted(identifiers)})
    elif action == "restore":
        for archive in archives.values():
            db.delete(archive)
        if manual_ids:
            for archive in db.scalars(select(MLDatasetSampleArchive).where(MLDatasetSampleArchive.sample_id.in_(manual_ids))).all():
                db.delete(archive)
        audit(db, actor, "ml_sample", actor.id, "ML_SAMPLES_RESTORED", {"source_keys": sorted(identifiers)})
    else:
        raise HTTPException(422, "Неизвестное действие")
    db.commit()
    return RedirectResponse("/web/ml/dataset-create", 303)


@router.post("/ml/datasets")
def freeze_dataset_page(
    request: Request,
    sample_ids: list[str] = Form(default=[]),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    try:
        row = freeze_dataset_version(db, actor.id, sample_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(db, actor, "dataset", row.id, "ML_DATASET_FROZEN", {"version": row.version})
    db.commit()
    return RedirectResponse("/web/ml", 303)


@router.get("/ml/datasets/{version}.json")
def download_dataset_page(
    version: int,
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    require_role(actor, Role.ADMIN)
    row = db.scalar(select(DatasetVersion).where(DatasetVersion.version == version))
    if not row:
        raise HTTPException(404, "Версия датасета не найдена")
    return Response(
        row.manifest,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="compost-dataset-v{version}.json"'},
    )


@router.post("/ml/train")
def queue_training_page(
    request: Request,
    dataset_version: int = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    dataset = db.scalar(select(DatasetVersion).where(DatasetVersion.version == dataset_version))
    if not dataset:
        raise HTTPException(404, "Версия датасета не найдена")
    active = db.scalar(
        select(ModelTrainingRun).where(ModelTrainingRun.status.in_(["QUEUED", "RUNNING"]))
    )
    if active:
        raise HTTPException(409, "Обучение уже поставлено в очередь")
    readiness = training_readiness(json.loads(dataset.manifest))
    if not readiness["privacy_safe"]:
        raise HTTPException(422, "Эта версия использует старую или небезопасную схему данных и запрещена для обучения")
    if not readiness["trainable"]:
        raise HTTPException(422, "Недостаточно данных даже для экспериментального обучения")
    run = ModelTrainingRun(dataset_version=dataset.version, requested_by=actor.id)
    db.add(run)
    db.flush()
    audit(db, actor, "ml_training", run.id, "ML_TRAINING_QUEUED", {"dataset_version": dataset.version})
    db.commit()
    return RedirectResponse("/web/ml", 303)


@router.post("/ml/runs/{run_id}/deploy")
def deploy_training_run_page(
    request: Request,
    run_id: str,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    run = db.get(ModelTrainingRun, run_id)
    if not run or run.status != "PASSED" or not run.artifact_prefix:
        raise HTTPException(409, "Кандидат не прошёл контроль качества")
    metrics = json.loads(run.metrics or "{}")
    dataset = db.scalar(select(DatasetVersion).where(DatasetVersion.version == run.dataset_version))
    readiness = training_readiness(json.loads(dataset.manifest)) if dataset else {"deployable": False}
    if not metrics.get("production_eligible") or not readiness["deployable"]:
        raise HTTPException(409, "Экспериментальную модель нельзя публиковать без независимой тестовой выборки")
    client = boto3.client(
        "s3",
        endpoint_url=get_settings().s3_endpoint,
        aws_access_key_id=get_settings().s3_access_key,
        aws_secret_access_key=get_settings().s3_secret_key,
    )
    for name in ("model.onnx", "report.json"):
        client.copy_object(
            Bucket=get_settings().s3_bucket,
            CopySource={"Bucket": get_settings().s3_bucket, "Key": f"{run.artifact_prefix}/{name}"},
            Key=f"ml/active/{name}",
        )
    active = json.dumps(
        {"run_id": run.id, "dataset_version": run.dataset_version, "deployed_at": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False,
    ).encode()
    client.put_object(
        Bucket=get_settings().s3_bucket,
        Key="ml/active/active.json",
        Body=active,
        ContentType="application/json",
    )
    run.deployed_at = datetime.now(timezone.utc)
    run.deployed_by = actor.id
    audit(db, actor, "ml_training", run.id, "ML_MODEL_DEPLOYED", {"dataset_version": run.dataset_version})
    db.commit()
    return RedirectResponse("/web/ml", 303)


@router.get("/incidents")
def incidents_page(request: Request, status: str = Query("ACTIVE"), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    return RedirectResponse(f"/web/violations?status={status}", 303)


@router.get("/violations")
def violations_page(request: Request, status: str = Query("ACTIVE"), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    query = select(Violation).order_by(Violation.created_at.desc())
    if status == "ACTIVE":
        query = query.where(Violation.is_active.is_(True))
    rows = list(db.scalars(query).all())
    actor_ids = {value for row in rows for value in (row.user_id, row.created_by, row.resolved_by) if value}
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    reviews = {review.id: review for review in db.scalars(select(Review).where(Review.id.in_({row.review_id for row in rows}))).all()} if rows else {}
    sessions = {session.id: session for session in db.scalars(select(AccessSession).where(AccessSession.id.in_({review.session_id for review in reviews.values()}))).all()} if reviews else {}
    cards = [{
        "id": row.id,
        "kind": "USER_VIOLATION",
        "violation": row,
        "active": row.is_active,
        "reason": row.reason,
        "created_at": row.created_at,
        "resolved_at": row.resolved_at,
        "user": users.get(row.user_id),
        "source": users.get(row.created_by),
        "resolver": users.get(row.resolved_by),
        "composter": db.get(Composter, sessions[reviews[row.review_id].session_id].composter_id) if row.review_id in reviews and reviews[row.review_id].session_id in sessions else None,
        "photo_url": photo_url(reviews[row.review_id].photo_key) if row.review_id in reviews else None,
        "annotations": json.loads(reviews[row.review_id].annotations or "[]") if row.review_id in reviews else [],
    } for row in rows]
    incident_query = select(Incident).order_by(Incident.created_at.desc())
    if status == "ACTIVE":
        incident_query = incident_query.where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))
    incidents = list(db.scalars(incident_query).all())
    incident_actor_ids = {value for row in incidents for value in (row.created_by, row.resolved_by) if value}
    incident_users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(incident_actor_ids))).all()} if incident_actor_ids else {}
    cards.extend({
        "id": row.id,
        "kind": row.kind,
        "violation": None,
        "active": row.status in ("OPEN", "IN_PROGRESS"),
        "reason": row.description or row.title,
        "created_at": row.created_at,
        "resolved_at": row.resolved_at,
        "user": None,
        "source": incident_users.get(row.created_by),
        "resolver": incident_users.get(row.resolved_by),
        "composter": db.get(Composter, row.composter_id) if row.composter_id else None,
        "photo_url": photo_url(row.photo_key) if row.photo_key else None,
        "annotations": [],
    } for row in incidents)
    cards.sort(key=lambda row: row["created_at"], reverse=True)
    composters = list(db.scalars(select(Composter).order_by(Composter.name)).all())
    return templates.TemplateResponse(request, "violations.html", page_context(request, actor, db, "violations", violations=cards, status=status, composters=composters))


@router.post("/violations/create")
def create_violation_web(
    request: Request,
    composter_id: str = Form(""),
    kind: str = Form("CONTAMINATION"),
    severity: str = Form("MEDIUM"),
    description: str = Form(""),
    file: UploadFile | None = File(None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    actor: User = Depends(web_current_user),
):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    composter = db.get(Composter, composter_id) if composter_id else None
    if composter_id and not composter:
        raise HTTPException(404, "Composter not found")
    kind = kind.strip().upper()
    severity = severity.strip().upper()
    title = incident_title(kind)
    description = description.strip()
    if len(kind) < 2 or len(kind) > 80 or severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        raise HTTPException(422, "Некорректный тип или приоритет")
    if len(title) < 3 or len(title) > 255 or len(description) > 5000:
        raise HTTPException(422, "Проверьте заголовок и описание")
    photo_key = None
    if file and file.filename:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(415, "Можно загружать только изображения")
        photo_key = store_photo(file)
    incident = Incident(
        composter_id=composter.id if composter else None,
        kind=kind,
        severity=severity,
        title=title,
        description=description,
        photo_key=photo_key,
        created_by=actor.id,
    )
    db.add(incident)
    db.flush()
    if incident.kind == "OVERFLOW" and composter:
        composter.needs_emptying = True
    audit(db, actor, "incident", incident.id, "INCIDENT_CREATED", {
        "composter_id": incident.composter_id,
        "kind": incident.kind,
        "severity": incident.severity,
        "photo_key": photo_key,
    })
    if composter:
        audit(db, actor, "composter", composter.id, "INCIDENT_CREATED", {"incident_id": incident.id, "kind": incident.kind})
    db.commit()
    return RedirectResponse(f"/web/violations/{incident.id}", 303)


@router.get("/violations/{item_id}")
def violation_detail(request: Request, item_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER, Role.MODERATOR)
    violation = db.get(Violation, item_id)
    incident = None if violation else db.get(Incident, item_id)
    if not violation and not incident:
        raise HTTPException(404, "Violation not found")
    if violation:
        review = db.get(Review, violation.review_id)
        session = db.get(AccessSession, review.session_id) if review else None
        composter = db.get(Composter, session.composter_id) if session else None
        user = db.get(User, violation.user_id)
        score = db.get(UserScore, violation.user_id)
        transactions = list(db.scalars(select(ScoreTransaction).where(ScoreTransaction.review_id == violation.review_id).order_by(ScoreTransaction.created_at.desc())).all())
        entity_ids = [violation.id, violation.review_id]
        photo_key = review.photo_key if review else None
        annotations = json.loads(review.annotations or "[]") if review else []
        created_by = db.get(User, violation.created_by) if violation.created_by else None
        resolved_by = db.get(User, violation.resolved_by) if violation.resolved_by else None
    else:
        review = session = user = score = None
        composter = db.get(Composter, incident.composter_id) if incident.composter_id else None
        transactions = []
        entity_ids = [incident.id]
        photo_key = incident.photo_key
        annotations = []
        created_by = db.get(User, incident.created_by) if incident.created_by else None
        resolved_by = db.get(User, incident.resolved_by) if incident.resolved_by else None
    audits = list(db.scalars(select(AuditLog).where(AuditLog.entity_id.in_(entity_ids)).order_by(AuditLog.created_at.desc())).all())
    audit_actor_ids = {row.user_id for row in audits if row.user_id}
    audit_actors = {row.id: row for row in db.scalars(select(User).where(User.id.in_(audit_actor_ids))).all()} if audit_actor_ids else {}
    return templates.TemplateResponse(
        request,
        "violation_detail.html",
        page_context(
            request,
            actor,
            db,
            "violations",
            violation=violation,
            incident=incident,
            review=review,
            session=session,
            composter=composter,
            subject_user=user,
            score=score,
            transactions=transactions,
            photo_key=photo_key,
            photo_url_value=photo_url(photo_key) if photo_key else None,
            annotations=annotations,
            created_by=created_by,
            resolved_by=resolved_by,
            audits=audits,
            audit_actors=audit_actors,
        ),
    )


@router.post("/violations/{violation_id}/resolve")
def resolve_violation_web(request: Request, violation_id: str, csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
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
        audit_incident_resolved(db, actor, incident, "WEB")
        db.commit()
        return RedirectResponse("/web/violations", 303)
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
    return RedirectResponse("/web/violations", 303)


@router.get("/analytics")
def analytics_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN)
    reviewed = db.scalar(select(func.count(Review.id)).where(Review.status != ReviewStatus.PENDING)) or 0
    approved = db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.APPROVED)) or 0
    rejected = db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.REJECTED)) or 0
    users = db.scalar(select(func.count(User.id)).where(User.role == Role.USER)) or 0
    return templates.TemplateResponse(request, "analytics.html", page_context(request, actor, db, "analytics", reviewed=reviewed, approved=approved, rejected=rejected, approval_rate=round(approved / reviewed * 100, 1) if reviewed else 0, users=users))


@router.get("/flasher")
def flasher_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    return templates.TemplateResponse(request, "flasher.html", page_context(request, actor, db, "flasher", firmwares=firmware_rows()))


@router.post("/flasher/firmwares")
async def upload_firmware(request: Request, file: UploadFile = File(), address: str = Form("0x10000"), chip: str = Form("ESP32"), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".bin"):
        raise HTTPException(415, "Можно загружать только файлы .bin")
    try:
        parsed_address = int(address, 0)
    except ValueError:
        raise HTTPException(422, "Некорректный адрес прошивки")
    if parsed_address < 0:
        raise HTTPException(422, "Некорректный адрес прошивки")
    data = await file.read(32 * 1024 * 1024 + 1)
    if not data or len(data) > 32 * 1024 * 1024:
        raise HTTPException(413, "Файл прошивки должен быть от 1 байта до 32 МБ")
    key = f"firmwares/{uuid.uuid4()}/{filename}"
    firmware_s3().put_object(
        Bucket=get_settings().firmware_s3_bucket,
        Key=key,
        Body=data,
        ContentType="application/octet-stream",
        Metadata={"address": hex(parsed_address), "chip": chip.strip()[:80] or "ESP32"},
    )
    audit(db, actor, "firmware", key, "FIRMWARE_UPLOADED", {"name": filename, "address": hex(parsed_address), "chip": chip, "size": len(data)})
    db.commit()
    return RedirectResponse("/web/flasher", 303)


@router.get("/flasher/firmwares/{firmware_key:path}")
def download_firmware(firmware_key: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    if not firmware_key.startswith("firmwares/"):
        raise HTTPException(404, "Firmware not found")
    response = firmware_s3().get_object(Bucket=get_settings().firmware_s3_bucket, Key=firmware_key)
    return StreamingResponse(response["Body"], media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{Path(firmware_key).name}"'})


@router.post("/flasher/firmwares/{firmware_key:path}/delete")
def delete_firmware(request: Request, firmware_key: str, csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    if not firmware_key.startswith("firmwares/"):
        raise HTTPException(404, "Firmware not found")
    firmware_s3().delete_object(Bucket=get_settings().firmware_s3_bucket, Key=firmware_key)
    audit(db, actor, "firmware", firmware_key, "FIRMWARE_DELETED")
    db.commit()
    return RedirectResponse("/web/flasher", 303)


@router.post("/flasher/audit")
async def flasher_audit(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.ENGINEER)
    if request.headers.get("x-csrf-token") != request.cookies.get("compost_csrf"):
        raise HTTPException(403, "CSRF validation failed")
    data = await request.json()
    audit(db, actor, "firmware", actor.id, "WEB_FLASH_COMPLETED" if data.get("success") else "WEB_FLASH_FAILED", {
        "chip": str(data.get("chip", ""))[:120],
        "files": data.get("files", [])[:20],
        "erase_all": bool(data.get("erase_all")),
        "error": str(data.get("error", ""))[:500],
    })
    db.commit()
    return {"status": "logged"}


@router.post("/reviews/{review_id}")
def moderate_review(request: Request, review_id: str, action: str = Form(), comment: str = Form(""), incident_kind: str = Form(""), annotations: str = Form("[]"), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.MODERATOR)
    if action not in {"APPROVE", "CONTAMINATION", "INCIDENT"}:
        raise HTTPException(422, "Unknown moderation action")
    approved = action != "CONTAMINATION"
    create_incident = action == "INCIDENT"
    review = db.get(Review, review_id)
    if review and review.status == ReviewStatus.PENDING:
        review.status = ReviewStatus.APPROVED if approved else ReviewStatus.REJECTED
        review.comment = (comment.strip() or None) if create_incident else (None if approved else "Обнаружено загрязнение")
        boxes = normalized_annotations(annotations)
        review.annotations = json.dumps(boxes, ensure_ascii=False)
        review.reviewed_by = actor.id
        review.reviewed_at = datetime.now(timezone.utc)
        session = db.get(AccessSession, review.session_id)
        user = db.get(User, session.user_id)
        if create_incident:
            try:
                incident = create_moderation_incident(db, actor, session.composter_id, incident_kind, comment, review.id, review.photo_key)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if incident is None:
                raise HTTPException(422, "Incident type is required")
        was_blocked = user.is_blocked
        if not approved:
            review.violation_reason = "CONTAMINATION"
            apply_violation(db, review, session, review.violation_reason)
            if not was_blocked and user.is_blocked:
                audit(db, actor, "user", user.id, "USER_AUTO_BLOCKED", {"review_id": review.id})
        reward_review(db, session, approved)
        audit(db, actor, "review", review.id, "REVIEW_APPROVED" if approved else "REVIEW_REJECTED")
        db.commit()
    return RedirectResponse("/web/moderation", 303)


@router.get("/map")
def map_page(request: Request, focus: str = Query(""), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    composters = list(db.scalars(select(Composter).order_by(Composter.name)).all())
    markers = [{"id": item.id, "name": item.name, "latitude": item.latitude, "longitude": item.longitude, "available": item.is_available, "full": item.needs_emptying} for item in composters]
    return templates.TemplateResponse(request, "map.html", page_context(request, actor, db, "map", markers=markers, focus=focus))
