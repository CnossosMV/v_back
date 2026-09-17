"""Harden event sources for MES v3.

Revision ID: 133_event_source_mes_v3
Revises: 132_attr_touches
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa


revision = "133_event_source_mes_v3"
down_revision = "132_attr_touches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messaging_events",
        "source",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default=None,
    )

    op.execute(sa.text("""
        UPDATE messaging_events
        SET source = 'system'
        WHERE source = 'frontend'
          AND event_name IN ('message_received', 'message_routed', 'message_replied')
          AND user_agent IS NULL
          AND ip_address IS NULL
    """))

    op.execute(sa.text("""
        UPDATE messaging_events AS event
        SET source = 'sandbox'
        FROM messaging_users AS contact
        WHERE event.user_id = contact.id
          AND event.source = 'frontend'
          AND contact.is_sandbox = true
          AND event.user_agent IS NULL
          AND event.ip_address IS NULL
    """))

    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_msg_evt_attr_send_created
        ON messaging_events (
            project_id,
            ((attribution ->> 'send_log_id')),
            created_at
        )
        WHERE attribution ? 'send_log_id'
    """))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_msg_evt_attr_send_created"
    ))
    op.alter_column(
        "messaging_events",
        "source",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default=sa.text("'frontend'"),
    )
