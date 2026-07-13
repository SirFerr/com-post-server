import secrets

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import Composter, Role, User
from .security import hash_password


def seed() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        accounts = [("admin@example.com", "Admin123!", Role.ADMIN, "Администратор"), ("moderator@example.com", "Moderator123!", Role.MODERATOR, "Модератор"), ("engineer@example.com", "Engineer123!", Role.ENGINEER, "Инженер"), ("user@example.com", "User123!", Role.USER, "Пользователь")]
        for email, password, role, full_name in accounts:
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, password_hash=hash_password(password), role=role, full_name=full_name))
        if not db.scalar(select(Composter).limit(1)):
            db.add_all([
                Composter(name="Комposter at university", device_id="esp32-demo-1", latitude=55.7558, longitude=37.6173, secret=secrets.token_hex(32)),
                Composter(name="Комposter in park", device_id="esp32-demo-2", latitude=55.7510, longitude=37.6180, secret=secrets.token_hex(32)),
            ])
        db.commit()


if __name__ == "__main__":
    seed()
