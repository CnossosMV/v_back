"""Add send_layer tables

Revision ID: e4f5a6b7_055_send_layer
Revises: d3e4f5a6_054_webhook_src
Create Date: 2026-03-01 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'e4f5a6b7_055_send_layer'
down_revision = 'd3e4f5a6_054_webhook_src'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    # ── send_logs ──────────────────────────────────────────────────
    if not table_exists("send_logs"):
        op.create_table(
            "send_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("channel", sa.String(50), nullable=False),
            sa.Column("recipient", sa.String(255), nullable=False),
            sa.Column("content_type", sa.String(30), nullable=False),
            sa.Column("content_summary", sa.String(500), nullable=True),
            sa.Column("content_payload", JSONB, nullable=True),
            sa.Column("template_id", sa.Integer(), sa.ForeignKey("messaging_templates.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_type", sa.String(50), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=True),
            sa.Column("decision_trace", JSONB, nullable=True),
            sa.Column("preferred_channel", sa.String(50), nullable=True),
            sa.Column("resolved_channel", sa.String(50), nullable=True),
            sa.Column("fallback_order", JSONB, nullable=True),
            sa.Column("fallback_attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
            sa.Column("provider_message_id", sa.String(255), nullable=True),
            sa.Column("provider_response", JSONB, nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("queued_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("failed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_send_logs_proj_status", "send_logs", ["project_id", "status"])
        op.create_index("ix_send_logs_proj_user", "send_logs", ["project_id", "user_id"])
        op.create_index("ix_send_logs_source", "send_logs", ["source_type", "source_id"])
        op.create_index("ix_send_logs_provider_msg", "send_logs", ["provider_message_id"])
        op.create_index(
            "ix_send_logs_scheduled",
            "send_logs",
            ["scheduled_at"],
            postgresql_where=sa.text("scheduled_at IS NOT NULL"),
        )

    # ── project_send_configs ──────────────────────────────────────
    if not table_exists("project_send_configs"):
        op.create_table(
            "project_send_configs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("default_strategy", sa.String(30), nullable=False, server_default="try_fallback"),
            sa.Column("default_fallback_order", JSONB, nullable=False, server_default='["whatsapp","email","sms"]'),
            sa.Column("rate_limit_messages", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("rate_limit_window_minutes", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("quiet_hours_enabled", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("quiet_hours_start", sa.String(5), nullable=True),
            sa.Column("quiet_hours_end", sa.String(5), nullable=True),
            sa.Column("quiet_hours_timezone", sa.String(50), nullable=False, server_default="UTC"),
            sa.Column("quiet_hours_action", sa.String(20), nullable=False, server_default="delay"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", name="uq_send_cfg_project"),
        )

    # ── channel_capabilities ──────────────────────────────────────
    if not table_exists("channel_capabilities"):
        op.create_table(
            "channel_capabilities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(50), nullable=False),
            sa.Column("display_name", sa.String(100), nullable=False),
            sa.Column("max_text_length", sa.Integer(), nullable=True),
            sa.Column("supports_media", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("supported_media_types", JSONB, nullable=True),
            sa.Column("max_media_size_mb", sa.Integer(), nullable=True),
            sa.Column("supports_buttons", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("max_buttons", sa.Integer(), nullable=True),
            sa.Column("supports_templates", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("supports_rich_text", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("supports_reactions", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("has_session_window", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("session_window_hours", sa.Integer(), nullable=True),
            sa.Column("requires_opt_in", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("supports_read_receipts", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("metadata", JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("channel", name="uq_chan_cap_channel"),
        )

    # Seed 4 channels (outside table_exists check — create_all may pre-create empty table)
    op.execute("""
            INSERT INTO channel_capabilities
                (channel, display_name, max_text_length, supports_media, supported_media_types,
                 max_media_size_mb, supports_buttons, max_buttons, supports_templates,
                 supports_rich_text, supports_reactions, has_session_window,
                 session_window_hours, requires_opt_in, supports_read_receipts)
            VALUES
                ('whatsapp', 'WhatsApp', 4096, true,
                 '["image","video","audio","document"]', 16, true, 3, true,
                 false, true, true, 24, false, true),
                ('email', 'Email', NULL, true,
                 '["image","document"]', 25, false, NULL, true,
                 true, false, false, NULL, false, false),
                ('sms', 'SMS', 160, false,
                 NULL, NULL, false, NULL, false,
                 false, false, false, NULL, false, false),
                ('web', 'Web Chat', NULL, true,
                 '["image","video","audio","document"]', 50, true, 10, false,
                 true, true, false, NULL, false, true)
            ON CONFLICT (channel) DO NOTHING;
        """)

    # ── delivery_status_events ────────────────────────────────────
    if not table_exists("delivery_status_events"):
        op.create_table(
            "delivery_status_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("send_log_id", sa.Integer(), sa.ForeignKey("send_logs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("provider_status", sa.String(100), nullable=True),
            sa.Column("provider_timestamp", sa.DateTime(), nullable=True),
            sa.Column("error_code", sa.String(50), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("raw_payload", JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_del_status_send_log", "delivery_status_events", ["send_log_id"])


def downgrade() -> None:
    op.drop_table("delivery_status_events")
    op.drop_table("channel_capabilities")
    op.drop_table("project_send_configs")
    op.drop_table("send_logs")
