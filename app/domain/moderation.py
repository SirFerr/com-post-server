from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AccessSession, Review, ScoreTransaction, User, Violation
from .scores import locked_score


def apply_violation(db: Session, review: Review, session: AccessSession, reason: str) -> None:
    existing = db.scalar(select(Violation).where(Violation.review_id == review.id))
    if not existing:
        db.add(Violation(
            user_id=session.user_id,
            review_id=review.id,
            reason=reason,
            created_by=review.reviewed_by,
        ))
        db.flush()
    active = db.scalar(
        select(func.count(Violation.id)).where(
            Violation.user_id == session.user_id,
            Violation.is_active.is_(True),
        )
    ) or 0
    if active >= 3:
        user = db.get(User, session.user_id)
        user.is_blocked = True
        user.ban_reason = "Three active composting violations"


def reward_review(db: Session, session: AccessSession, approved: bool) -> None:
    score = locked_score(db, session.user_id)
    score.total_uploads += 1
    if approved:
        score.valid_uploads += 1
        score.points += 10
        amount, reason = 10, "Загрузка одобрена"
    else:
        previous = score.points
        score.points = max(0, score.points - 15)
        amount, reason = score.points - previous, "Загрузка отклонена"
    db.flush()
    review_id = db.scalar(select(Review.id).where(Review.session_id == session.id))
    db.add(ScoreTransaction(
        user_id=session.user_id,
        amount=amount,
        balance_after=score.points,
        reason=reason,
        review_id=review_id,
    ))
