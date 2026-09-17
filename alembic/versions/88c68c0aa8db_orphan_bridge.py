"""orphan_bridge - Bridge for orphaned revision

Revision ID: 88c68c0aa8db
Revises: j7d8e9f0g1h2
Create Date: 2026-02-05 18:00:00.000000

This is a bridge migration to fix orphaned database states.
When production was deployed, it created revision 88c68c0aa8db which was
later renamed/removed. This bridge allows databases at 88c68c0aa8db to
continue the migration chain properly.

This migration is a no-op since the schema changes it represented
are now handled by d5bb3a39fb14_add_smart_analytics_tables.py
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '88c68c0aa8db'
down_revision = 'j7d8e9f0g1h2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: This is a bridge migration for orphaned database states
    # The actual schema changes are handled by d5bb3a39fb14
    pass


def downgrade() -> None:
    # No-op
    pass
