"""Add project setup plans and explicit market configuration.

Revision ID: 153_project_setup
Revises: 152_campaign_recipes
Create Date: 2026-08-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "153_project_setup"
down_revision = "152_campaign_recipes"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(op.get_bind()).get_columns(table))


def _index_exists(table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _column_exists("projects", "market_config"):
        op.add_column("projects", sa.Column("market_config", postgresql.JSONB(), nullable=True))

    if not _table_exists("project_setup_plans"):
        op.create_table(
            "project_setup_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
            sa.Column("plan_data", postgresql.JSONB(), nullable=False),
            sa.Column("assessment_snapshot", postgresql.JSONB(), nullable=True),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "version", name="uq_project_setup_plan_version"),
            sa.CheckConstraint(
                "status IN ('draft','in_progress','completed','superseded','archived')",
                name="ck_project_setup_plan_status",
            ),
        )
    if not _index_exists("project_setup_plans", "ix_project_setup_plans_project_status"):
        op.create_index("ix_project_setup_plans_project_status", "project_setup_plans", ["project_id", "status"])

    if not _table_exists("project_setup_decisions"):
        op.create_table(
            "project_setup_decisions",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("project_setup_plans.id", ondelete="CASCADE"), nullable=False),
            sa.Column("decision_key", sa.String(160), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("evidence_kind", sa.String(30), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("value", postgresql.JSONB(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("sources", postgresql.JSONB(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("plan_id", "decision_key", "revision", name="uq_project_setup_decision_revision"),
            sa.CheckConstraint(
                "evidence_kind IN ('observed','inferred','recommended','tenant_decided')",
                name="ck_project_setup_decision_evidence",
            ),
            sa.CheckConstraint(
                "status IN ('proposed','accepted','rejected','superseded')",
                name="ck_project_setup_decision_status",
            ),
        )
    for name, columns in (
        ("ix_project_setup_decisions_plan_key", ["plan_id", "decision_key"]),
        ("ix_project_setup_decisions_project", ["project_id"]),
    ):
        if not _index_exists("project_setup_decisions", name):
            op.create_index(name, "project_setup_decisions", columns)

    if not _table_exists("project_setup_phase_approvals"):
        op.create_table(
            "project_setup_phase_approvals",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("project_setup_plans.id", ondelete="CASCADE"), nullable=False),
            sa.Column("phase_key", sa.String(120), nullable=False),
            sa.Column("plan_fingerprint", sa.String(64), nullable=False),
            sa.Column("approval_note", sa.Text(), nullable=False),
            sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "plan_id", "phase_key", "plan_fingerprint",
                name="uq_project_setup_phase_approval_fingerprint",
            ),
        )
    for name, columns in (
        ("ix_project_setup_phase_approvals_plan_phase", ["plan_id", "phase_key"]),
        ("ix_project_setup_phase_approvals_project", ["project_id"]),
    ):
        if not _index_exists("project_setup_phase_approvals", name):
            op.create_index(name, "project_setup_phase_approvals", columns)


def downgrade() -> None:
    if _table_exists("project_setup_phase_approvals"):
        op.drop_table("project_setup_phase_approvals")
    if _table_exists("project_setup_decisions"):
        op.drop_table("project_setup_decisions")
    if _table_exists("project_setup_plans"):
        op.drop_table("project_setup_plans")
    if _column_exists("projects", "market_config"):
        op.drop_column("projects", "market_config")
