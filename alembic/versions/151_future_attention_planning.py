"""Add bounded future attention planning to outbound intents.

Revision ID: 151_future_attention_planning
Revises: 150_outbound_source_contract
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "151_future_attention_planning"
down_revision = "150_outbound_source_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("send_logs", sa.Column("attention_policy_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("send_logs", sa.Column("planning_status", sa.String(length=30), nullable=True))
    op.add_column("send_logs", sa.Column("planning_evaluated_at", sa.DateTime(), nullable=True))
    op.add_column("send_logs", sa.Column("planning_horizon_end", sa.DateTime(), nullable=True))
    op.add_column("send_logs", sa.Column("planning_snapshot", postgresql.JSONB(), nullable=True))
    op.create_index("ix_send_logs_planning_status", "send_logs", ["planning_status"], unique=False)
    op.create_index(
        "ix_send_logs_attention_schedule",
        "send_logs",
        ["project_id", "user_id", "attention_scope", "status", "scheduled_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_send_logs_attention_schedule", table_name="send_logs")
    op.drop_index("ix_send_logs_planning_status", table_name="send_logs")
    op.drop_column("send_logs", "planning_snapshot")
    op.drop_column("send_logs", "planning_horizon_end")
    op.drop_column("send_logs", "planning_evaluated_at")
    op.drop_column("send_logs", "planning_status")
    op.drop_column("send_logs", "attention_policy_snapshot")
