"""Outbound send-pace governor: domains, rate state, caps

Revision ID: 123_send_pace
Revises: 122_mes_v2
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = '123_send_pace'
down_revision = '122_mes_v2'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def index_exists(index_name: str, table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    # --- sending_domains: first-class reputation unit + pace policy ---
    if not table_exists("sending_domains"):
        op.create_table(
            "sending_domains",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("domain", sa.String(length=255), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=True),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("max_per_minute", sa.Integer(), nullable=True),
            sa.Column("max_per_day", sa.Integer(), nullable=True),
            sa.Column("throttle_factor", sa.Float(), server_default=sa.text("0.3"), nullable=False),
            sa.Column("warmup_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("warmup_started_at", sa.Date(), nullable=True),
            sa.Column("warmup_schedule", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
            sa.Column("bounce_rate_24h", sa.Float(), nullable=True),
            sa.Column("complaint_rate_24h", sa.Float(), nullable=True),
            sa.Column("reputation_updated_at", sa.DateTime(), nullable=True),
            sa.Column("auto_throttle_bounce_pct", sa.Float(), server_default=sa.text("2.0"), nullable=False),
            sa.Column("auto_pause_bounce_pct", sa.Float(), server_default=sa.text("5.0"), nullable=False),
            sa.Column("auto_pause_complaint_pct", sa.Float(), server_default=sa.text("0.3"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("domain", name="uq_sending_domain"),
        )
    if not index_exists("ix_sending_domains_domain", "sending_domains"):
        op.create_index("ix_sending_domains_domain", "sending_domains", ["domain"])
    if not index_exists("ix_sending_domains_workspace_id", "sending_domains"):
        op.create_index("ix_sending_domains_workspace_id", "sending_domains", ["workspace_id"])
    if not index_exists("ix_sending_domains_project_id", "sending_domains"):
        op.create_index("ix_sending_domains_project_id", "sending_domains", ["project_id"])

    # --- sending_rate_state: leaky-bucket cursor + daily counter per domain ---
    if not table_exists("sending_rate_state"):
        op.create_table(
            "sending_rate_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scope_key", sa.String(length=255), nullable=False),
            sa.Column("next_slot_at", sa.DateTime(), nullable=True),
            sa.Column("day", sa.Date(), nullable=True),
            sa.Column("day_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope_key", name="uq_rate_state_scope"),
        )
    if not index_exists("ix_sending_rate_state_scope_key", "sending_rate_state"):
        op.create_index("ix_sending_rate_state_scope_key", "sending_rate_state", ["scope_key"])

    # --- project_send_configs: project-level pace fallback ---
    if not column_exists("project_send_configs", "max_per_minute"):
        op.add_column("project_send_configs", sa.Column("max_per_minute", sa.Integer(), nullable=True))
    if not column_exists("project_send_configs", "max_per_day"):
        op.add_column("project_send_configs", sa.Column("max_per_day", sa.Integer(), nullable=True))

    # --- send_logs: resolved sending domain (pace/reputation key) ---
    if not column_exists("send_logs", "sending_domain"):
        op.add_column("send_logs", sa.Column("sending_domain", sa.String(length=255), nullable=True))
    if not index_exists("ix_send_logs_sending_domain", "send_logs"):
        op.create_index("ix_send_logs_sending_domain", "send_logs", ["sending_domain"])

    # --- delivery_feedback: sending domain attribution (for reputation) ---
    if not column_exists("delivery_feedback", "sending_domain"):
        op.add_column("delivery_feedback", sa.Column("sending_domain", sa.String(length=255), nullable=True))
    if not index_exists("ix_dlvry_fdbk_domain", "delivery_feedback"):
        op.create_index("ix_dlvry_fdbk_domain", "delivery_feedback", ["sending_domain", "feedback_type", "created_at"])


def downgrade() -> None:
    if index_exists("ix_dlvry_fdbk_domain", "delivery_feedback"):
        op.drop_index("ix_dlvry_fdbk_domain", table_name="delivery_feedback")
    if column_exists("delivery_feedback", "sending_domain"):
        op.drop_column("delivery_feedback", "sending_domain")
    if index_exists("ix_send_logs_sending_domain", "send_logs"):
        op.drop_index("ix_send_logs_sending_domain", table_name="send_logs")
    if column_exists("send_logs", "sending_domain"):
        op.drop_column("send_logs", "sending_domain")
    if column_exists("project_send_configs", "max_per_day"):
        op.drop_column("project_send_configs", "max_per_day")
    if column_exists("project_send_configs", "max_per_minute"):
        op.drop_column("project_send_configs", "max_per_minute")
    if table_exists("sending_rate_state"):
        op.drop_table("sending_rate_state")
    if table_exists("sending_domains"):
        op.drop_table("sending_domains")
