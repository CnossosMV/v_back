"""078 funnel sandbox tables + is_sandbox flag

Revision ID: h8i9j0k1_078_fnl_sandbox
Revises: f6g7h8i9_077_tpl_folder
Create Date: 2026-03-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'h8i9j0k1_078_fnl_sandbox'
down_revision = 'f6g7h8i9_077_tpl_folder'
branch_labels = None
depends_on = None


def table_exists(conn, name):
    insp = inspect(conn)
    return name in insp.get_table_names()


def column_exists(conn, table, column):
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def index_exists(conn, name):
    insp = inspect(conn)
    for tbl in insp.get_table_names():
        for idx in insp.get_indexes(tbl):
            if idx["name"] == name:
                return True
    return False


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add is_sandbox column to messaging_users
    if not column_exists(conn, "messaging_users", "is_sandbox"):
        op.add_column(
            "messaging_users",
            sa.Column("is_sandbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )
    if not index_exists(conn, "ix_msg_users_project_sandbox"):
        op.create_index(
            "ix_msg_users_project_sandbox",
            "messaging_users",
            ["project_id", "is_sandbox"],
        )

    # 2. Create sandbox_sessions table
    if not table_exists(conn, "sandbox_sessions"):
        op.create_table(
            "sandbox_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("funnel_id", sa.Integer(), sa.ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("contact_id", sa.Integer(), sa.ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("mode", sa.String(20), server_default="log_only", nullable=False),  # log_only, preview
            sa.Column("preview_channel", sa.String(30), nullable=True),  # whatsapp, email
            sa.Column("preview_destination", sa.String(255), nullable=True),  # phone or email for preview
            sa.Column("preview_instance_id", sa.Integer(), nullable=True),  # WhatsApp instance for preview
            sa.Column("status", sa.String(20), server_default="active", nullable=False),  # active, completed, reset
            sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("funnel_enrollments.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("mode IN ('log_only', 'preview')", name="ck_sandbox_session_mode"),
            sa.CheckConstraint("status IN ('active', 'completed', 'reset')", name="ck_sandbox_session_status"),
        )
        op.create_index(
            "ix_sandbox_sessions_funnel_contact",
            "sandbox_sessions",
            ["funnel_id", "contact_id", "status"],
        )

    # 3. Create sandbox_action_logs table
    if not table_exists(conn, "sandbox_action_logs"):
        op.create_table(
            "sandbox_action_logs",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("sandbox_sessions.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("enrollment_id", sa.Integer(), nullable=True),
            sa.Column("step_id", sa.Integer(), nullable=True),
            sa.Column("action_type", sa.String(50), nullable=False),
            sa.Column("action_config", sa.JSON(), nullable=True),
            sa.Column("intercepted_mode", sa.String(30), nullable=False),  # logged, preview_sent, preview_failed
            sa.Column("preview_result", sa.JSON(), nullable=True),
            sa.Column("resolved_variables", sa.JSON(), nullable=True),
            sa.Column("suppression_check", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if table_exists(conn, "sandbox_action_logs"):
        op.drop_table("sandbox_action_logs")

    if table_exists(conn, "sandbox_sessions"):
        op.drop_table("sandbox_sessions")

    if index_exists(conn, "ix_msg_users_project_sandbox"):
        op.drop_index("ix_msg_users_project_sandbox", table_name="messaging_users")

    if column_exists(conn, "messaging_users", "is_sandbox"):
        op.drop_column("messaging_users", "is_sandbox")
