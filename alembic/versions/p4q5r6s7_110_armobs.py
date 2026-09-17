"""Add arm_observations evidence ledger (Phase 4 bandit)

Revision ID: p4q5r6s7_110_armobs
Revises: o3p4q5r6_109_slot_id
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'p4q5r6s7_110_armobs'
down_revision = 'o3p4q5r6_109_slot_id'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("arm_observations"):
        return
    op.create_table(
        "arm_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_class", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("arm_key", sa.String(length=160), nullable=False),
        sa.Column("arm_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("send_log_id", sa.Integer(),
                  sa.ForeignKey("send_logs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reward", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        # Bandit decision classes are execution-only by schema (intent/episode
        # selection has NO arm identity — enforces "intent never reward-decided").
        sa.CheckConstraint(
            "decision_class IN ('variant','channel','send_time','tie_break')",
            name="ck_arm_obs_decision_class",
        ),
        # Append-once: one observation per send per decision class.
        sa.UniqueConstraint("send_log_id", "decision_class", name="uq_arm_obs_send_class"),
    )
    op.create_index("ix_arm_obs_scope", "arm_observations",
                    ["project_id", "scope_key", "arm_key"])
    op.create_index("ix_arm_obs_observed", "arm_observations",
                    ["project_id", "observed_at"])


def downgrade() -> None:
    if _has_table("arm_observations"):
        op.drop_table("arm_observations")
