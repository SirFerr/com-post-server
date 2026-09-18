import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..domain.reviews import decide_review
from ..models import AccessSession, Review, ReviewStatus, Role, User, Violation
from ..schemas import ModerateRequest
from ..security import require_roles
from ..services import audit, photo_url

router = APIRouter(prefix="/moderation")


def review_payload(db: Session, review: Review) -> dict:
    session = db.get(AccessSession, review.session_id)
    reviewer = db.get(User, review.reviewed_by) if review.reviewed_by else None
    return {
        "id": review.id,
        "session_id": review.session_id,
        "photo_url": photo_url(review.photo_key),
        "status": review.status,
        "ml_status": review.ml_status,
        "confidence": review.ml_confidence,
        "ml_violations": json.loads(review.ml_violations),
        "annotations": json.loads(review.annotations or "[]"),
        "created_at": review.created_at,
        "reviewed_at": review.reviewed_at,
        "comment": review.comment,
        "violation_reason": review.violation_reason,
        "reviewer_name": (reviewer.full_name or reviewer.email) if reviewer else None,
        "user_name": session.user.full_name or session.user.email,
        "user_email": session.user.email,
        "composter_id": session.composter.id,
        "composter_name": session.composter.name,
    }


@router.get("/reviews")
def pending_reviews(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    reviews = db.scalars(select(Review).where(Review.status == ReviewStatus.PENDING).order_by(Review.created_at)).all()
    return [review_payload(db, review) for review in reviews]


@router.get("/history")
def moderation_history(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    reviews = db.scalars(
        select(Review)
        .where(Review.status != ReviewStatus.PENDING)
        .order_by(Review.reviewed_at.desc(), Review.created_at.desc())
        .limit(200)
    ).all()
    return [review_payload(db, review) for review in reviews]


@router.post("/reviews/{review_id}")
def moderate(review_id: str, data: ModerateRequest, db: Session = Depends(get_db), moderator: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    return decide_review(db, moderator, review_id, approved=data.approved,
                         create_incident=data.create_incident, incident_kind=data.incident_kind,
                         comment=data.comment, annotations=data.annotations)


@router.post("/violations/{violation_id}/cancel")
def cancel_violation(violation_id: str, db: Session = Depends(get_db), moderator: User = Depends(require_roles(Role.MODERATOR, Role.ADMIN))):
    violation = db.scalar(select(Violation).where(Violation.id == violation_id).with_for_update())
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
