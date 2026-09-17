"""Add MCP origin metadata

Revision ID: l1m2n3o4_106_mcp_origin
Revises: k0l1m2n3_105_mcp
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "l1m2n3o4_106_mcp_origin"
down_revision = "k0l1m2n3_105_mcp"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    conn_cols = _columns("mcp_connector_installations")
    if "allowed_origins" not in conn_cols:
        op.add_column(
            "mcp_connector_installations",
            sa.Column(
                "allowed_origins",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        )

    audit_cols = _columns("mcp_tool_audit_logs")
    if "origin" not in audit_cols:
        op.add_column(
            "mcp_tool_audit_logs",
            sa.Column("origin", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    audit_cols = _columns("mcp_tool_audit_logs")
    if "origin" in audit_cols:
        op.drop_column("mcp_tool_audit_logs", "origin")

    conn_cols = _columns("mcp_connector_installations")
    if "allowed_origins" in conn_cols:
        op.drop_column("mcp_connector_installations", "allowed_origins")
