"""Add best-time-to-send window columns.

Revision ID: 135_send_windows
Revises: 134_contact_pause
Create Date: 2026-07-25

"""
from alembic import op
import sqlalchemy as sa


revision = "135_send_windows"
down_revision = "134_contact_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE project_send_configs "
        "ADD COLUMN IF NOT EXISTS send_windows JSON NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE messaging_users "
        "ADD COLUMN IF NOT EXISTS send_windows JSONB NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE messaging_users DROP COLUMN IF EXISTS send_windows"))
    op.execute(sa.text("ALTER TABLE project_send_configs DROP COLUMN IF EXISTS send_windows"))
