"""Add email_instances table

Revision ID: k2l3m4n5_081_email_inst
Revises: j1k2l3m4_080_fork_thrd
Create Date: 2026-03-18

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'k2l3m4n5_081_email_inst'
down_revision = 'j1k2l3m4_080_fork_thrd'
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return name in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("email_instances"):
        return

    op.create_table(
        "email_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        # Provider discriminator
        sa.Column("provider_type", sa.String(30), nullable=False, server_default="smtp"),
        sa.Column("instance_name", sa.String(100), nullable=False),
        sa.Column("from_email", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(100), nullable=True),
        # SMTP columns
        sa.Column("smtp_server", sa.String(255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_username", sa.String(255), nullable=True),
        sa.Column("smtp_password_enc", sa.Text(), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("smtp_use_ssl", sa.Boolean(), server_default=sa.text("false")),
        # API columns
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("api_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        # Lifecycle
        sa.Column("connection_status", sa.String(20), server_default="pending"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
    )

    op.create_index("ix_email_inst_workspace", "email_instances", ["workspace_id"])
    op.create_index("ix_email_inst_project", "email_instances", ["project_id"])
    op.create_unique_constraint("uq_email_inst_name", "email_instances", ["instance_name"])


def downgrade() -> None:
    if not _table_exists("email_instances"):
        return
    op.drop_table("email_instances")
