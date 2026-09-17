"""Compile any EventActions missing their system Funnel.

Revision ID: 126_compile_orphan_event_actions
Revises: 125_tabloide_i18n
Create Date: 2026-07-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from alembic_event_action_compiler import compile_event_action_for_migration


revision = "126_compile_orphan_event_actions"
down_revision = "125_tabloide_i18n"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    orphaned_ids = conn.execute(sa.text(
        """
        SELECT ea.id
        FROM event_actions ea
        LEFT JOIN funnels f ON f.event_action_id = ea.id
        WHERE f.id IS NULL
        ORDER BY ea.id
        """
    )).scalars().all()
    for event_action_id in orphaned_ids:
        compile_event_action_for_migration(conn, int(event_action_id))


def downgrade() -> None:
    # Existing EventActions may predate this migration. Their compiled Funnels
    # are intentionally retained on downgrade to avoid deleting active sends.
    pass
