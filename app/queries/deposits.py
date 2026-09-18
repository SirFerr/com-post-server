from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import AccessSession, Review


def user_deposits(db: Session, user_id: str):
    """Load the history and its display data in one bounded query."""
    return db.execute(
        select(AccessSession, Review)
        .outerjoin(Review, Review.session_id == AccessSession.id)
        .options(joinedload(AccessSession.composter))
        .where(AccessSession.user_id == user_id)
        .order_by(AccessSession.created_at.desc())
        .limit(100)
    ).all()
