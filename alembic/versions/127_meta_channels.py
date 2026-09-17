"""Meta page connections + messenger/instagram channels

Revision ID: 127_meta_channels
Revises: 126_compile_orphan_event_actions
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "127_meta_channels"
down_revision = "126_compile_orphan_event_actions"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("meta_page_connections"):
        op.create_table(
            "meta_page_connections",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "project_id",
                sa.Integer(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("page_id", sa.String(64), nullable=False),
            sa.Column("page_name", sa.String(255), nullable=True),
            sa.Column("page_access_token_enc", sa.Text(), nullable=True),
            sa.Column("ig_account_id", sa.String(64), nullable=True),
            sa.Column("ig_username", sa.String(255), nullable=True),
            sa.Column("subscribed_fields", sa.JSON(), nullable=True),
            sa.Column("messenger_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("instagram_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("fb_comments_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("ig_comments_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("status", sa.String(30), nullable=False, server_default="connected"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "connected_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", "page_id", name="uq_meta_page_conn_proj_page"),
        )
        op.create_index("ix_meta_page_conn_page_id", "meta_page_connections", ["page_id"])
        op.create_index("ix_meta_page_conn_ig_id", "meta_page_connections", ["ig_account_id"])

    if not _table_exists("meta_oauth_sessions"):
        op.create_table(
            "meta_oauth_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("state", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column(
                "project_id",
                sa.Integer(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("user_token_enc", sa.Text(), nullable=True),
            sa.Column("pages_json", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )

    if not _table_exists("meta_messaging_windows"):
        op.create_table(
            "meta_messaging_windows",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "connection_id",
                sa.Integer(),
                sa.ForeignKey("meta_page_connections.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("platform", sa.String(20), nullable=False),
            sa.Column("contact_id", sa.String(64), nullable=False),
            sa.Column("window_opens_at", sa.DateTime(), nullable=False),
            sa.Column("window_expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint(
                "connection_id", "platform", "contact_id", name="uq_meta_window_conn_plat_contact"
            ),
        )
        op.create_index("ix_meta_window_expires", "meta_messaging_windows", ["window_expires_at"])

    # Seed channel capabilities (idempotent)
    op.execute("""
        INSERT INTO channel_capabilities (
            channel, display_name, max_text_length,
            supports_media, supported_media_types, max_media_size_mb,
            supports_buttons, max_buttons, supports_templates,
            supports_rich_text, supports_reactions,
            has_session_window, session_window_hours,
            requires_opt_in, supports_read_receipts,
            supported_statuses, icon_hint,
            is_inbound_capable, inbound_requires_setup
        )
        SELECT 'messenger', 'Messenger', 2000,
               true, '["image","video","audio","file"]'::json, 25,
               true, 3, false,
               false, true,
               true, 24,
               false, true,
               '["sent","delivered","read","failed"]'::jsonb, 'MessageCircle',
               true, true
        WHERE NOT EXISTS (
            SELECT 1 FROM channel_capabilities WHERE channel = 'messenger'
        )
    """)
    op.execute("""
        INSERT INTO channel_capabilities (
            channel, display_name, max_text_length,
            supports_media, supported_media_types, max_media_size_mb,
            supports_buttons, max_buttons, supports_templates,
            supports_rich_text, supports_reactions,
            has_session_window, session_window_hours,
            requires_opt_in, supports_read_receipts,
            supported_statuses, icon_hint,
            is_inbound_capable, inbound_requires_setup
        )
        SELECT 'instagram', 'Instagram', 1000,
               true, '["image","video","audio"]'::json, 25,
               false, NULL, false,
               false, true,
               true, 24,
               false, true,
               '["sent","delivered","read","failed"]'::jsonb, 'Instagram',
               true, true
        WHERE NOT EXISTS (
            SELECT 1 FROM channel_capabilities WHERE channel = 'instagram'
        )
    """)


def downgrade() -> None:
    op.execute("DELETE FROM channel_capabilities WHERE channel IN ('messenger', 'instagram')")
    for table in ("meta_messaging_windows", "meta_oauth_sessions", "meta_page_connections"):
        if _table_exists(table):
            op.drop_table(table)
