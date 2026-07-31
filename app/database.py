from sqlalchemy import create_engine, inspect, text
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


def migrate_schema() -> None:
    """Small idempotent migrations for installations created before Alembic."""
    from .models import ModelTrainingRun

    ModelTrainingRun.__table__.create(engine, checkfirst=True)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("reviews")}
    statements = []
    if "annotations" not in columns:
        statements.append("ALTER TABLE reviews ADD COLUMN annotations TEXT NOT NULL DEFAULT '[]'")
    if "reviewed_at" not in columns:
        statements.append("ALTER TABLE reviews ADD COLUMN reviewed_at TIMESTAMP NULL")
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "ban_until" not in user_columns:
        statements.append("ALTER TABLE users ADD COLUMN ban_until TIMESTAMP NULL")
    if "warning_message" not in user_columns:
        statements.append("ALTER TABLE users ADD COLUMN warning_message VARCHAR(500) NULL")
    composter_columns = {column["name"] for column in inspector.get_columns("composters")}
    if "maintenance_mode" not in composter_columns:
        statements.append("ALTER TABLE composters ADD COLUMN maintenance_mode BOOLEAN NOT NULL DEFAULT FALSE")
    incident_columns = {column["name"] for column in inspector.get_columns("incidents")}
    if "photo_key" not in incident_columns:
        statements.append("ALTER TABLE incidents ADD COLUMN photo_key VARCHAR(512) NULL")
    violation_columns = {column["name"] for column in inspector.get_columns("violations")}
    if "created_by" not in violation_columns:
        statements.append("ALTER TABLE violations ADD COLUMN created_by VARCHAR(36) NULL REFERENCES users(id)")
    if "resolved_by" not in violation_columns:
        statements.append("ALTER TABLE violations ADD COLUMN resolved_by VARCHAR(36) NULL REFERENCES users(id)")
    if "resolved_at" not in violation_columns:
        statements.append("ALTER TABLE violations ADD COLUMN resolved_at TIMESTAMP NULL")
    incident_columns = {column["name"] for column in inspector.get_columns("incidents")} if inspector.has_table("incidents") else set()
    if incident_columns and "resolved_by" not in incident_columns:
        statements.append("ALTER TABLE incidents ADD COLUMN resolved_by VARCHAR(36) NULL REFERENCES users(id)")
    audit_columns = {column["name"]: column for column in inspector.get_columns("audit_logs")}
    entity_id_type = audit_columns.get("entity_id", {}).get("type")
    if engine.dialect.name == "postgresql" and getattr(entity_id_type, "length", 0) < 512:
        statements.append("ALTER TABLE audit_logs ALTER COLUMN entity_id TYPE VARCHAR(512)")
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
