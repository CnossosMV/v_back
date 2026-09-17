"""Add per-contact automation pause fields.

Revision ID: 134_contact_pause
Revises: 133_event_source_mes_v3
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa


revision = "134_contact_pause"
down_revision = "133_event_source_mes_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE messaging_users "
        "ADD COLUMN IF NOT EXISTS automations_paused BOOLEAN NOT NULL DEFAULT false"
    ))
    op.execute(sa.text(
        "ALTER TABLE messaging_users "
        "ADD COLUMN IF NOT EXISTS automations_paused_at TIMESTAMP NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE messaging_users "
        "ADD COLUMN IF NOT EXISTS automations_paused_reason VARCHAR(255) NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE messaging_users "
        "ADD COLUMN IF NOT EXISTS automations_pause_mode VARCHAR(10) NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_msg_users_project_auto_paused "
        "ON messaging_users (project_id, automations_paused)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_msg_users_project_auto_paused"))
    op.execute(sa.text("ALTER TABLE messaging_users DROP COLUMN IF EXISTS automations_pause_mode"))
    op.execute(sa.text("ALTER TABLE messaging_users DROP COLUMN IF EXISTS automations_paused_reason"))
    op.execute(sa.text("ALTER TABLE messaging_users DROP COLUMN IF EXISTS automations_paused_at"))
    op.execute(sa.text("ALTER TABLE messaging_users DROP COLUMN IF EXISTS automations_paused"))
