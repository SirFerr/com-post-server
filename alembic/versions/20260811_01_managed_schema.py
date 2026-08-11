"""Adopt the current schema and add revocable-user fields."""
from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401

revision = "20260811_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "token_version" not in columns:
            batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
        if "deleted_at" not in columns:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("token_version")
