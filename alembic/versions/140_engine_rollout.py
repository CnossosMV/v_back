"""Add per-project engine rollout

Revision ID: 140_engine_rollout
Revises: 139_campaign_override
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "140_engine_rollout"
down_revision = "139_campaign_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    if "project_engine_rollouts" not in existing:
        op.create_table(
            "project_engine_rollouts",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("feature_key", sa.String(80), nullable=False),
            sa.Column("mode", sa.String(20), server_default="inherit", nullable=False),
            sa.Column("config", postgresql.JSONB(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("project_id", "feature_key", name="uq_engine_rollout_feature"),
        )
        op.create_index("ix_engine_rollout_project", "project_engine_rollouts", ["project_id", "feature_key"])
    if "project_engine_rollout_events" not in existing:
        op.create_table(
            "project_engine_rollout_events",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("feature_key", sa.String(80), nullable=False),
            sa.Column("previous_mode", sa.String(20), nullable=True),
            sa.Column("new_mode", sa.String(20), nullable=False),
            sa.Column("previous_config", postgresql.JSONB(), nullable=True),
            sa.Column("new_config", postgresql.JSONB(), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        )
        op.create_index("ix_engine_rollout_event_project", "project_engine_rollout_events", ["project_id", "created_at"])


def downgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    if "project_engine_rollout_events" in existing:
        op.drop_table("project_engine_rollout_events")
    if "project_engine_rollouts" in existing:
        op.drop_table("project_engine_rollouts")
