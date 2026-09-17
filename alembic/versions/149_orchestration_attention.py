"""Add deterministic attention policy to orchestration sources.

Revision ID: 149_orchestration_attention
Revises: 148_commercial_opportunity
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "149_orchestration_attention"
down_revision = "148_commercial_opportunity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("funnels", sa.Column("attention_policy", postgresql.JSONB(), nullable=True))
    op.add_column("event_actions", sa.Column("attention_policy", postgresql.JSONB(), nullable=True))
    op.add_column("campaigns", sa.Column("attention_policy", postgresql.JSONB(), nullable=True))
    op.add_column("send_logs", sa.Column("attention_scope", sa.String(length=120), nullable=True))
    op.add_column("send_logs", sa.Column("purpose_key", sa.String(length=120), nullable=True))
    op.create_index("ix_send_logs_attention_scope", "send_logs", ["attention_scope"], unique=False)
    op.create_index("ix_send_logs_purpose_key", "send_logs", ["purpose_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_send_logs_purpose_key", table_name="send_logs")
    op.drop_index("ix_send_logs_attention_scope", table_name="send_logs")
    op.drop_column("send_logs", "purpose_key")
    op.drop_column("send_logs", "attention_scope")
    op.drop_column("campaigns", "attention_policy")
    op.drop_column("event_actions", "attention_policy")
    op.drop_column("funnels", "attention_policy")
