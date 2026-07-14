from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import Role, User
from .security import hash_password


ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Admin1234"


def seed() -> None:
    """Create the single bootstrap administrator in an otherwise empty database."""
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.email == ADMIN_EMAIL)):
            db.add(User(
                email=ADMIN_EMAIL,
                password_hash=hash_password(ADMIN_PASSWORD),
                role=Role.ADMIN,
                full_name="Администратор",
            ))
        db.commit()


if __name__ == "__main__":
    seed()
