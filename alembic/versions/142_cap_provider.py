"""Add capability and provider state

Revision ID: 142_cap_provider
Revises: 141_decision_gate
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "142_cap_provider"
down_revision = "141_decision_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    if "provider_operational_states" not in existing:
        op.create_table(
            "provider_operational_states", sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("profile_type", sa.String(50), nullable=False), sa.Column("profile_id", sa.String(120), nullable=False),
            sa.Column("channel", sa.String(50), nullable=False), sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("status", sa.String(30), nullable=False), sa.Column("quota", postgresql.JSONB(), nullable=True),
            sa.Column("usage", postgresql.JSONB(), nullable=True), sa.Column("capabilities", postgresql.JSONB(), nullable=True),
            sa.Column("last_success_at", sa.DateTime(), nullable=True), sa.Column("last_error_at", sa.DateTime(), nullable=True),
            sa.Column("last_error_code", sa.String(120), nullable=True), sa.Column("source", sa.String(80), nullable=False),
            sa.Column("observed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.Column("stale_after", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("project_id", "profile_type", "profile_id", name="uq_provider_operational_state"),
        )
        op.create_index("ix_provider_state_project", "provider_operational_states", ["project_id", "channel", "status"])
    if "capability_executions" not in existing:
        op.create_table(
            "capability_executions", sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("capability_key", sa.String(120), nullable=False), sa.Column("phase", sa.String(20), nullable=False),
            sa.Column("idempotency_key", sa.String(255), nullable=False), sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("decision_choice_id", sa.BigInteger(), sa.ForeignKey("decision_gate_choices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("input_payload", postgresql.JSONB(), nullable=False),
            sa.Column("output_payload", postgresql.JSONB(), nullable=True), sa.Column("status", sa.String(30), nullable=False),
            sa.Column("error", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("project_id", "capability_key", "idempotency_key", name="uq_capability_execution_key"),
        )
        op.create_index("ix_capability_execution_project", "capability_executions", ["project_id", "created_at"])


def downgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())
    for table in ("capability_executions", "provider_operational_states"):
        if table in existing: op.drop_table(table)
