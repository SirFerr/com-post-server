from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User, UserScore


def locked_score(db: Session, user_id: str) -> UserScore:
    # Lock the parent, which exists even before the first score is created.
    # Every score writer must use this operation within its transaction.
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise HTTPException(404, "User not found")
    score = db.scalar(
        select(UserScore).where(UserScore.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if score is None:
        score = UserScore(user_id=user_id, points=0, total_uploads=0, valid_uploads=0)
        db.add(score)
    return score
