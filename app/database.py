from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


url = get_settings().database_url
engine_options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
if url == "sqlite://":
    engine_options["poolclass"] = StaticPool
engine = create_engine(url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
