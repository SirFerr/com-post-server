from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Composter, Incident, Review, ReviewStatus, User, Violation


def dashboard_stats(db: Session) -> dict:
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "blocked_users": db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0,
        "composters": db.scalar(select(func.count(Composter.id))) or 0,
        "available_composters": db.scalar(select(func.count(Composter.id)).where(Composter.is_available.is_(True))) or 0,
        "full_composters": db.scalar(select(func.count(Composter.id)).where(Composter.needs_emptying.is_(True))) or 0,
        "low_battery_composters": db.scalar(select(func.count(Composter.id)).where(Composter.battery_level <= 20)) or 0,
        "pending_reviews": db.scalar(select(func.count(Review.id)).where(Review.status == ReviewStatus.PENDING)) or 0,
        "active_violations": (db.scalar(select(func.count(Violation.id)).where(Violation.is_active.is_(True))) or 0)
        + (db.scalar(select(func.count(Incident.id)).where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))) or 0),
        "open_incidents": db.scalar(select(func.count(Incident.id)).where(Incident.status.in_(["OPEN", "IN_PROGRESS"]))) or 0,
    }


def page_context(request: Request, actor: User, db: Session, active: str, **values) -> dict:
    return {
        "actor": actor,
        "stats": dashboard_stats(db),
        "csrf_token": request.cookies.get("compost_csrf", ""),
        "active": active,
        **values,
    }
