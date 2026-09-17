"""Add persisted decision gates

Revision ID: 141_decision_gate
Revises: 140_engine_rollout
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "141_decision_gate"
down_revision = "140_engine_rollout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind(); existing = set(inspect(bind).get_table_names())
    if "decision_gate_evaluations" not in existing:
        op.create_table(
            "decision_gate_evaluations",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("gate_type", sa.String(80), nullable=False), sa.Column("subject_type", sa.String(80), nullable=False),
            sa.Column("subject_id", sa.String(120), nullable=False), sa.Column("subject_version", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(30), server_default="valid", nullable=False),
            sa.Column("method", sa.String(20), server_default="exact", nullable=False),
            sa.Column("inputs_hash", sa.String(64), nullable=False),
            sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("baseline", postgresql.JSONB(), nullable=False), sa.Column("options", postgresql.JSONB(), nullable=False),
            sa.Column("provenance", postgresql.JSONB(), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        )
        op.create_index("ix_decision_gate_subject", "decision_gate_evaluations", ["project_id", "gate_type", "subject_type", "subject_id"])
        op.create_index("ix_decision_gate_expiry", "decision_gate_evaluations", ["project_id", "status", "expires_at"])
    if "decision_gate_choices" not in existing:
        op.create_table(
            "decision_gate_choices",
            sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("evaluation_id", sa.BigInteger(), sa.ForeignKey("decision_gate_evaluations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("option_key", sa.String(80), nullable=False), sa.Column("consequence_snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True), sa.Column("chosen_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("chosen_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("execution_type", sa.String(80), nullable=True), sa.Column("execution_id", sa.String(120), nullable=True),
            sa.UniqueConstraint("project_id", "evaluation_id", name="uq_decision_gate_choice"),
        )
        op.create_index("ix_decision_choice_project", "decision_gate_choices", ["project_id", "chosen_at"])
    if "decision_gate_outcomes" not in existing:
        op.create_table(
            "decision_gate_outcomes", sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("choice_id", sa.BigInteger(), sa.ForeignKey("decision_gate_choices.id", ondelete="CASCADE"), nullable=False),
            sa.Column("outcome", postgresql.JSONB(), nullable=False),
            sa.Column("reconciled_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("project_id", "choice_id", name="uq_decision_gate_outcome"),
        )
    columns = {c["name"] for c in inspect(bind).get_columns("campaign_runs")}
    if "decision_choice_id" not in columns:
        op.add_column("campaign_runs", sa.Column("decision_choice_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key("fk_campaign_run_decision_choice", "campaign_runs", "decision_gate_choices", ["decision_choice_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_campaign_runs_decision_choice_id", "campaign_runs", ["decision_choice_id"])


def downgrade() -> None:
    bind = op.get_bind(); existing = set(inspect(bind).get_table_names())
    if "campaign_runs" in existing and "decision_choice_id" in {c["name"] for c in inspect(bind).get_columns("campaign_runs")}:
        op.drop_column("campaign_runs", "decision_choice_id")
    for table in ("decision_gate_outcomes", "decision_gate_choices", "decision_gate_evaluations"):
        if table in existing: op.drop_table(table)
