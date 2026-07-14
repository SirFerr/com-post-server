import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import AccessSession, AuditLog, Composter, Review, ReviewStatus, Role, User, Violation
from .security import create_token, verify_password, web_current_user
from .services import apply_violation, audit, photo_url, reward_review

router = APIRouter(prefix="/web", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def csrf(request: Request, value: str) -> None:
    if not value or not secrets.compare_digest(value, request.cookies.get("compost_csrf", "")):
        raise HTTPException(403, "Invalid form token")


def require_role(actor: User, *roles: Role) -> None:
    if actor.role not in roles:
        raise HTTPException(403, "Недостаточно прав")


def dashboard_stats(db: Session) -> dict:
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "blocked_users": db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0,
        "composters": db.scalar(select(func.count(Composter.id))) or 0,
        "available_composters": db.scalar(select(func.count(Composter.id)).where(Composter.is_available.is_(True))) or 0,
        "full_composters": db.scalar(select(func.count(Composter.id)).where(Composter.needs_emptying.is_(True))) or 0,
        "pending_reviews": db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.PENDING)) or 0,
        "active_violations": db.scalar(select(func.count(Violation.id)).where(Violation.is_active.is_(True))) or 0,
    }


def page_context(request: Request, actor: User, db: Session, active: str, **values) -> dict:
    return {
        "actor": actor,
        "stats": dashboard_stats(db),
        "csrf_token": request.cookies.get("compost_csrf", ""),
        "active": active,
        **values,
    }


def history_rows(db: Session, conditions) -> list[dict]:
    rows = list(db.scalars(select(AuditLog).where(or_(*conditions)).order_by(AuditLog.created_at.desc()).limit(100)).all())
    actor_ids = {row.user_id for row in rows if row.user_id}
    actors = {user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    return [{"row": row, "actor": actors.get(row.user_id)} for row in rows]


def review_queue(db: Session) -> list[dict]:
    result = []
    for review in db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all():
        session = db.get(AccessSession, review.session_id)
        result.append({
            "review": review,
            "session": session,
            "photo_url": photo_url(review.photo_key),
            "violations": ", ".join(json.loads(review.ml_violations)),
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
    user = db.scalar(select(User).where(User.email == email.lower()))
    if not user or not verify_password(password, user.password_hash) or user.role == Role.USER:
        return templates.TemplateResponse(request, "login.html", {"error": "Неверные данные или у аккаунта нет доступа к панели"}, status_code=401)
    response = RedirectResponse("/web/dashboard", 303)
    response.set_cookie("compost_session", create_token(user), httponly=True, samesite="strict", max_age=3600)
    response.set_cookie("compost_csrf", secrets.token_urlsafe(24), httponly=True, samesite="strict", max_age=3600)
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
def equipment_page(request: Request, q: str = Query(""), status: str = Query("ALL"), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    composters = list(db.scalars(select(Composter).order_by(Composter.name)).all())
    needle = q.strip().lower()
    if needle:
        composters = [item for item in composters if needle in item.name.lower() or needle in item.device_id.lower()]
    if status == "ACTIVE":
        composters = [item for item in composters if item.is_available]
    elif status == "DISABLED":
        composters = [item for item in composters if not item.is_available]
    elif status == "FULL":
        composters = [item for item in composters if item.needs_emptying]
    return templates.TemplateResponse(request, "equipment.html", page_context(request, actor, db, "equipment", composters=composters, q=q, status=status))


@router.get("/equipment/{composter_id}")
def equipment_detail(request: Request, composter_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    history = history_rows(db, [and_(AuditLog.entity == "composter", AuditLog.entity_id == composter.id)])
    return templates.TemplateResponse(request, "equipment_detail.html", page_context(request, actor, db, "equipment", composter=composter, history=history))


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
        raise HTTPException(403, "Password confirmation failed")
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
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.needs_emptying = needs_emptying
    audit(db, actor, "composter", composter.id, "FULL_REPORTED" if needs_emptying else "FULL_REPORT_CLEARED")
    db.commit()
    return RedirectResponse("/web/moderation" if from_moderation else f"/web/equipment/{composter.id}", 303)


@router.get("/users")
def users_page(request: Request, q: str = Query(""), status: str = Query("ALL"), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN)
    users = list(db.scalars(select(User).order_by(User.created_at.desc())).all())
    needle = q.strip().lower()
    if needle:
        users = [item for item in users if needle in item.email.lower() or needle in item.full_name.lower()]
    if status == "ACTIVE":
        users = [item for item in users if not item.is_blocked]
    elif status == "BLOCKED":
        users = [item for item in users if item.is_blocked]
    elif status == "STAFF":
        users = [item for item in users if item.role != Role.USER]
    return templates.TemplateResponse(request, "users.html", page_context(request, actor, db, "users", users=users, q=q, status=status))


@router.get("/users/{user_id}")
def user_detail(request: Request, user_id: str, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    review_ids = list(db.scalars(select(Review.id).join(AccessSession, Review.session_id == AccessSession.id).where(AccessSession.user_id == user.id)).all())
    conditions = [and_(AuditLog.entity == "user", AuditLog.entity_id == user.id)]
    if review_ids:
        conditions.append(and_(AuditLog.entity == "review", AuditLog.entity_id.in_(review_ids)))
    return templates.TemplateResponse(request, "user_detail.html", page_context(request, actor, db, "users", user=user, history=history_rows(db, conditions), roles=list(Role)))


@router.post("/users/{user_id}")
def change_user(request: Request, user_id: str, role: str = Form(), blocked: bool = Form(False), ban_reason: str = Form(""), current_password: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN)
    if not verify_password(current_password, actor.password_hash):
        raise HTTPException(403, "Password confirmation failed")
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
    if user.role != previous_role:
        audit(db, actor, "user", user.id, "USER_ROLE_CHANGED", {"from": previous_role.value, "to": user.role.value})
    if user.is_blocked != previous_blocked:
        audit(db, actor, "user", user.id, "USER_BLOCKED" if user.is_blocked else "USER_UNBLOCKED", {"reason": user.ban_reason})
    db.commit()
    return RedirectResponse(f"/web/users/{user.id}", 303)


@router.get("/moderation")
def moderation_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    require_role(actor, Role.ADMIN, Role.MODERATOR)
    return templates.TemplateResponse(request, "moderation.html", page_context(request, actor, db, "moderation", reviews=review_queue(db)))


@router.post("/reviews/{review_id}")
def moderate_review(request: Request, review_id: str, approved: bool = Form(), comment: str = Form(""), violation_reason: str = Form("INVALID_COMPOST"), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    require_role(actor, Role.ADMIN, Role.MODERATOR)
    review = db.get(Review, review_id)
    if review and review.status == ReviewStatus.PENDING:
        review.status = ReviewStatus.APPROVED if approved else ReviewStatus.REJECTED
        review.comment = comment.strip() or None
        review.reviewed_by = actor.id
        session = db.get(AccessSession, review.session_id)
        user = db.get(User, session.user_id)
        was_blocked = user.is_blocked
        if not approved:
            review.violation_reason = violation_reason
            apply_violation(db, review, session, violation_reason)
            if not was_blocked and user.is_blocked:
                audit(db, actor, "user", user.id, "USER_AUTO_BLOCKED", {"review_id": review.id})
        reward_review(db, session, approved)
        audit(db, actor, "review", review.id, "REVIEW_APPROVED" if approved else "REVIEW_REJECTED")
        db.commit()
    return RedirectResponse("/web/moderation", 303)


@router.get("/map")
def map_page(request: Request, db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    composters = list(db.scalars(select(Composter).order_by(Composter.name)).all())
    markers = [{"id": item.id, "name": item.name, "latitude": item.latitude, "longitude": item.longitude, "available": item.is_available, "full": item.needs_emptying} for item in composters]
    return templates.TemplateResponse(request, "map.html", page_context(request, actor, db, "map", markers=markers))
