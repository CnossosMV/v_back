"""Add MCP connector tables

Revision ID: k0l1m2n3_105_mcp
Revises: j9k0l1m2_104_audience_webhooks
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "k0l1m2n3_105_mcp"
down_revision = "j9k0l1m2_104_audience_webhooks"
branch_labels = None
depends_on = None


def _inspector():
    return inspect(op.get_bind())


def _table_exists(name: str) -> bool:
    return name in _inspector().get_table_names()


def _index_exists(table: str, name: str) -> bool:
    if not _table_exists(table):
        return False
    return name in {idx["name"] for idx in _inspector().get_indexes(table)}


def _create_index_once(name: str, table: str, columns: list[str], unique: bool = False) -> None:
    if _table_exists(table) and not _index_exists(table, name):
        op.create_index(name, table, columns, unique=unique)


def _drop_index_once(name: str, table: str) -> None:
    if _index_exists(table, name):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    if not _table_exists("mcp_connector_installations"):
        op.create_table(
            "mcp_connector_installations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("connector_type", sa.String(length=20), server_default="product", nullable=False),
            sa.Column("client_name", sa.String(length=100), nullable=True),
            sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
            sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("connector_type IN ('product', 'admin')", name="ck_mcp_conn_type"),
            sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_mcp_conn_status"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_mcp_connector_installations_id", "mcp_connector_installations", ["id"])
    _create_index_once("ix_mcp_connector_installations_user_id", "mcp_connector_installations", ["user_id"])
    _create_index_once("ix_mcp_connector_installations_project_id", "mcp_connector_installations", ["project_id"])
    _create_index_once("ix_mcp_conn_user_type", "mcp_connector_installations", ["user_id", "connector_type"])

    if not _table_exists("mcp_tool_audit_logs"):
        op.create_table(
            "mcp_tool_audit_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("connector_installation_id", sa.Integer(), nullable=True),
            sa.Column("server_type", sa.String(length=20), nullable=False),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("input_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("output_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("correlation_id", sa.String(length=64), nullable=False),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("server_type IN ('product', 'admin')", name="ck_mcp_audit_server_type"),
            sa.CheckConstraint("status IN ('success', 'error', 'denied', 'pending_confirmation')", name="ck_mcp_audit_status"),
            sa.ForeignKeyConstraint(["connector_installation_id"], ["mcp_connector_installations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_mcp_tool_audit_logs_id", "mcp_tool_audit_logs", ["id"])
    _create_index_once("ix_mcp_tool_audit_logs_project_id", "mcp_tool_audit_logs", ["project_id"])
    _create_index_once("ix_mcp_tool_audit_logs_user_id", "mcp_tool_audit_logs", ["user_id"])
    _create_index_once("ix_mcp_tool_audit_logs_correlation_id", "mcp_tool_audit_logs", ["correlation_id"])
    _create_index_once("ix_mcp_audit_proj_created", "mcp_tool_audit_logs", ["project_id", "created_at"])
    _create_index_once("ix_mcp_audit_tool_created", "mcp_tool_audit_logs", ["tool_name", "created_at"])

    if not _table_exists("mcp_pending_actions"):
        op.create_table(
            "mcp_pending_actions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("server_type", sa.String(length=20), nullable=False),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("action_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("server_type IN ('product', 'admin')", name="ck_mcp_pending_server_type"),
            sa.CheckConstraint("status IN ('pending', 'confirmed', 'expired', 'cancelled')", name="ck_mcp_pending_status"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
        )
    _create_index_once("ix_mcp_pending_actions_id", "mcp_pending_actions", ["id"])
    _create_index_once("ix_mcp_pending_actions_project_id", "mcp_pending_actions", ["project_id"])
    _create_index_once("ix_mcp_pending_actions_user_id", "mcp_pending_actions", ["user_id"])
    _create_index_once("ix_mcp_pending_actions_token_hash", "mcp_pending_actions", ["token_hash"], unique=True)
    _create_index_once("ix_mcp_pending_user_tool", "mcp_pending_actions", ["user_id", "tool_name", "status"])


def downgrade() -> None:
    for name, table in (
        ("ix_mcp_pending_user_tool", "mcp_pending_actions"),
        ("ix_mcp_pending_actions_token_hash", "mcp_pending_actions"),
        ("ix_mcp_pending_actions_user_id", "mcp_pending_actions"),
        ("ix_mcp_pending_actions_project_id", "mcp_pending_actions"),
        ("ix_mcp_pending_actions_id", "mcp_pending_actions"),
        ("ix_mcp_audit_tool_created", "mcp_tool_audit_logs"),
        ("ix_mcp_audit_proj_created", "mcp_tool_audit_logs"),
        ("ix_mcp_tool_audit_logs_correlation_id", "mcp_tool_audit_logs"),
        ("ix_mcp_tool_audit_logs_user_id", "mcp_tool_audit_logs"),
        ("ix_mcp_tool_audit_logs_project_id", "mcp_tool_audit_logs"),
        ("ix_mcp_tool_audit_logs_id", "mcp_tool_audit_logs"),
        ("ix_mcp_conn_user_type", "mcp_connector_installations"),
        ("ix_mcp_connector_installations_project_id", "mcp_connector_installations"),
        ("ix_mcp_connector_installations_user_id", "mcp_connector_installations"),
        ("ix_mcp_connector_installations_id", "mcp_connector_installations"),
    ):
        _drop_index_once(name, table)

    for table in ("mcp_pending_actions", "mcp_tool_audit_logs", "mcp_connector_installations"):
        if _table_exists(table):
            op.drop_table(table)
