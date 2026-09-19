"""Add durable delivery queue for independently stored telemetry history."""

from alembic import op
import sqlalchemy as sa


revision = "20260919_04"
down_revision = "20260811_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("outbox_events"):
        return
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("destination", sa.String(50), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_destination", "outbox_events", ["destination"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_destination", table_name="outbox_events")
    op.drop_table("outbox_events")
