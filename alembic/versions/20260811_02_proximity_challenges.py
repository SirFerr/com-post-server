"""Add one-time proximity challenges."""

from alembic import op
import sqlalchemy as sa


revision = "20260811_02"
down_revision = "20260811_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("proximity_challenges"):
        return
    op.create_table(
        "proximity_challenges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("composter_id", sa.String(length=36), nullable=False),
        sa.Column("command_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["composter_id"], ["composters.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id"),
    )
    op.create_index("ix_proximity_challenges_user_id", "proximity_challenges", ["user_id"])
    op.create_index("ix_proximity_challenges_composter_id", "proximity_challenges", ["composter_id"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("proximity_challenges"):
        return
    op.drop_index("ix_proximity_challenges_composter_id", table_name="proximity_challenges")
    op.drop_index("ix_proximity_challenges_user_id", table_name="proximity_challenges")
    op.drop_table("proximity_challenges")
