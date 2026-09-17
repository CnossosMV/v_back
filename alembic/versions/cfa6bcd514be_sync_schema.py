"""sync_schema

Revision ID: cfa6bcd514be
Revises: d5bb3a39fb14
Create Date: 2026-01-27 00:43:51.844123

This migration is intentionally empty (no-op).
All schema changes were already covered by earlier migrations:
- chat_messages/chat_sessions columns: migration 022 (g4a5b6c7d8e9)
- customer_*_configs.project_id: migration 005 (c14000459412)
- messaging_users indexes: migration 015 (f7a8b9c0d1e2)

This migration exists to maintain the migration chain after production was manually synced.
"""
from alembic import op
import sqlalchemy as sa


revision = 'cfa6bcd514be'
down_revision = 'd5bb3a39fb14'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: all changes already applied by earlier migrations
    pass


def downgrade() -> None:
    # No-op
    pass
