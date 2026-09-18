import os

from sqlalchemy import select

from .database import SessionLocal
from .models import Role, User
from .security import hash_password


def seed() -> None:
    """Create the single bootstrap administrator in an otherwise empty database."""
    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or len(password) < 12:
        raise RuntimeError("Set BOOTSTRAP_ADMIN_EMAIL and a BOOTSTRAP_ADMIN_PASSWORD of at least 12 characters")
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.email == email)):
            db.add(User(
                email=email,
                password_hash=hash_password(password),
                role=Role.ADMIN,
                full_name="Администратор",
            ))
        db.commit()


if __name__ == "__main__":
    seed()
