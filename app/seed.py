import secrets

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import Composter, Role, User
from .security import hash_password


DEMO_COMPOSTER_ID = "00000000-0000-0000-0000-000000000001"
DEMO_DEVICE_ID = "esp32-demo-1"
DEMO_DEVICE_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def seed() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        accounts = [("admin@example.com", "Admin123!", Role.ADMIN, "Администратор"), ("moderator@example.com", "Moderator123!", Role.MODERATOR, "Модератор"), ("engineer@example.com", "Engineer123!", Role.ENGINEER, "Инженер"), ("user@example.com", "User123!", Role.USER, "Пользователь")]
        for email, password, role, full_name in accounts:
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, password_hash=hash_password(password), role=role, full_name=full_name))
        demo = db.scalar(select(Composter).where(Composter.device_id == DEMO_DEVICE_ID))
        if demo and demo.id != DEMO_COMPOSTER_ID:
            demo.device_id = f"esp32-demo-legacy-{demo.id[:8]}"
            demo.is_available = False
            demo = None
        if not demo:
            db.add(Composter(id=DEMO_COMPOSTER_ID, name="Компостер у университета", device_id=DEMO_DEVICE_ID, latitude=55.7558, longitude=37.6173, secret=DEMO_DEVICE_SECRET))
        else:
            # Public demo credentials are deliberately deterministic so the checked-in
            # mock-lock firmware can be exercised end to end. Never use them in production.
            demo.secret = DEMO_DEVICE_SECRET
        if not db.scalar(select(Composter).where(Composter.device_id == "esp32-demo-2")):
            db.add(Composter(name="Компостер в парке", device_id="esp32-demo-2", latitude=55.7510, longitude=37.6180, secret=secrets.token_hex(32)))
        db.commit()


if __name__ == "__main__":
    seed()
