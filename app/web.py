import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
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


def dashboard_stats(db: Session) -> dict:
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "blocked_users": db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0,
        "composters": db.scalar(select(func.count(Composter.id))) or 0,
        "available_composters": db.scalar(select(func.count(Composter.id)).where(Composter.is_available.is_(True))) or 0,
        "sessions": db.scalar(select(func.count(AccessSession.id))) or 0,
        "pending_reviews": db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.PENDING)) or 0,
        "active_violations": db.scalar(select(func.count(Violation.id)).where(Violation.is_active.is_(True))) or 0,
    }


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
    reviews = []
    if actor.role in {Role.ADMIN, Role.MODERATOR}:
        for review in db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all():
            session = db.get(AccessSession, review.session_id)
            reviews.append({"review": review, "session": session, "photo_url": photo_url(review.photo_key), "violations": ", ".join(json.loads(review.ml_violations))})
    users = db.scalars(select(User).order_by(User.created_at.desc())).all() if actor.role == Role.ADMIN else []
    composters = db.scalars(select(Composter).order_by(Composter.name)).all()
    audit_rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(50)).all() if actor.role == Role.ADMIN else []
    return templates.TemplateResponse(request, "dashboard.html", {
        "actor": actor,
        "stats": dashboard_stats(db),
        "reviews": reviews,
        "users": users,
        "composters": composters,
        "audit_rows": audit_rows,
        "csrf_token": request.cookies.get("compost_csrf", ""),
        "roles": list(Role),
    })


@router.post("/reviews/{review_id}")
def moderate_review(request: Request, review_id: str, approved: bool = Form(), comment: str = Form(""), violation_reason: str = Form("INVALID_COMPOST"), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    if actor.role not in {Role.ADMIN, Role.MODERATOR}:
        raise HTTPException(403, "Moderator access required")
    review = db.get(Review, review_id)
    if review and review.status == ReviewStatus.PENDING:
        review.status = ReviewStatus.APPROVED if approved else ReviewStatus.REJECTED
        review.comment = comment.strip() or None
        review.reviewed_by = actor.id
        session = db.get(AccessSession, review.session_id)
        if not approved:
            review.violation_reason = violation_reason
            apply_violation(db, review, session, violation_reason)
        reward_review(db, session, approved)
        audit(db, actor, "review", review.id, "REVIEW_APPROVED" if approved else "REVIEW_REJECTED")
        db.commit()
    return RedirectResponse("/web/dashboard#moderation", 303)


@router.post("/users/{user_id}")
def change_user(request: Request, user_id: str, role: str = Form(), blocked: bool = Form(False), ban_reason: str = Form(""), current_password: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    if actor.role != Role.ADMIN:
        raise HTTPException(403, "Administrator access required")
    if not verify_password(current_password, actor.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = Role(role)
    user.is_blocked = blocked
    user.ban_reason = ban_reason.strip() if blocked else None
    audit(db, actor, "user", user.id, "USER_UPDATED_FROM_WEB", {"role": role, "blocked": blocked})
    db.commit()
    return RedirectResponse("/web/dashboard#users", 303)


@router.post("/composters/{composter_id}")
def change_composter(request: Request, composter_id: str, available: bool = Form(False), current_password: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), actor: User = Depends(web_current_user)):
    csrf(request, csrf_token)
    if actor.role not in {Role.ADMIN, Role.ENGINEER}:
        raise HTTPException(403, "Engineer access required")
    if not verify_password(current_password, actor.password_hash):
        raise HTTPException(403, "Password confirmation failed")
    composter = db.get(Composter, composter_id)
    if not composter:
        raise HTTPException(404, "Composter not found")
    composter.is_available = available
    audit(db, actor, "composter", composter.id, "AVAILABILITY_UPDATED_FROM_WEB", {"available": available})
    db.commit()
    return RedirectResponse("/web/dashboard#equipment", 303)
