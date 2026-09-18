"""One transaction for API and web moderation decisions."""
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AccessSession, Review, ReviewStatus, User
from .audit import audit
from .incidents import create_moderation_incident
from .moderation import apply_violation, reward_review


def decide_review(db: Session, actor: User, review_id: str, *, approved: bool,
                  create_incident: bool = False, incident_kind: str | None = None,
                  comment: str | None = None, annotations: list | None = None):
    review = db.scalar(select(Review).where(Review.id == review_id).with_for_update())
    if not review:
        raise HTTPException(404, "Review not found")
    if review.status != ReviewStatus.PENDING:
        return {"status": review.status}
    if create_incident and not approved:
        raise HTTPException(422, "An incident cannot be combined with a contamination rejection")
    review.status = ReviewStatus.APPROVED if approved else ReviewStatus.REJECTED
    comment = (comment or "").strip()
    review.comment = (comment or None) if create_incident else (None if approved else "Обнаружено загрязнение")
    review.annotations = json.dumps(annotations or [], ensure_ascii=False)
    review.reviewed_by = actor.id
    review.reviewed_at = datetime.now(timezone.utc)
    session = db.get(AccessSession, review.session_id)
    user = db.scalar(select(User).where(User.id == session.user_id).with_for_update().execution_options(populate_existing=True))
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
    return {"status": review.status}
