"""Track background worker liveness for monitoring."""

from alembic import op
import sqlalchemy as sa


revision = "20260919_05"
down_revision = "20260919_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("worker_heartbeats"):
        return
    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(50), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
