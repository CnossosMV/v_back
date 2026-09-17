"""Add supported_statuses and icon_hint to channel_capabilities

Revision ID: s9t0u1v2_069_ch_registry
Revises: r8s9t0u1_068_inst_id_slog
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 's9t0u1v2_069_ch_registry'
down_revision = 'r8s9t0u1_068_inst_id_slog'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("channel_capabilities")}

    if "supported_statuses" not in cols:
        op.add_column(
            "channel_capabilities",
            sa.Column("supported_statuses", JSONB, nullable=True),
        )

    if "icon_hint" not in cols:
        op.add_column(
            "channel_capabilities",
            sa.Column("icon_hint", sa.String(50), nullable=True),
        )

    # Seed statuses and icon_hint for existing channels
    op.execute("""
        UPDATE channel_capabilities
        SET supported_statuses = '["sent","delivered","read","failed"]'::jsonb,
            icon_hint = 'MessageSquare'
        WHERE channel = 'whatsapp'
    """)
    op.execute("""
        UPDATE channel_capabilities
        SET supported_statuses = '["sent","opened","failed","bounced"]'::jsonb,
            icon_hint = 'Mail'
        WHERE channel = 'email'
    """)
    op.execute("""
        UPDATE channel_capabilities
        SET supported_statuses = '["sent","delivered","failed"]'::jsonb,
            icon_hint = 'Phone'
        WHERE channel = 'sms'
    """)
    op.execute("""
        UPDATE channel_capabilities
        SET supported_statuses = '["sent","delivered","read","failed"]'::jsonb,
            icon_hint = 'Globe'
        WHERE channel = 'web'
    """)

    # Insert inapp and push if missing
    op.execute("""
        INSERT INTO channel_capabilities (
            channel, display_name, supports_media, supports_buttons,
            supports_templates, supports_rich_text, supports_reactions,
            has_session_window, requires_opt_in, supports_read_receipts,
            supported_statuses, icon_hint
        )
        SELECT 'inapp', 'In-App', false, false,
               false, false, false,
               false, false, true,
               '["sent","read"]'::jsonb, 'Bell'
        WHERE NOT EXISTS (
            SELECT 1 FROM channel_capabilities WHERE channel = 'inapp'
        )
    """)
    op.execute("""
        INSERT INTO channel_capabilities (
            channel, display_name, supports_media, supports_buttons,
            supports_templates, supports_rich_text, supports_reactions,
            has_session_window, requires_opt_in, supports_read_receipts,
            supported_statuses, icon_hint
        )
        SELECT 'push', 'Push', false, true,
               false, false, false,
               false, true, false,
               '["sent","delivered","failed"]'::jsonb, 'BellRing'
        WHERE NOT EXISTS (
            SELECT 1 FROM channel_capabilities WHERE channel = 'push'
        )
    """)


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("channel_capabilities")}

    # Remove inapp and push rows
    op.execute("DELETE FROM channel_capabilities WHERE channel IN ('inapp', 'push')")

    if "icon_hint" in cols:
        op.drop_column("channel_capabilities", "icon_hint")
    if "supported_statuses" in cols:
        op.drop_column("channel_capabilities", "supported_statuses")
