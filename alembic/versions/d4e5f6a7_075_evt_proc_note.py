"""075 add processing_notes to events

Revision ID: d4e5f6a7_075_evt_proc_note
Revises: a4c5d6e7_043_tpl_body_fmt
Create Date: 2026-03-13 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'd4e5f6a7_075_evt_proc_note'
down_revision = 'a4c5d6e7_043_tpl_body_fmt'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('messaging_events', 'processing_notes'):
        op.add_column(
            'messaging_events',
            sa.Column('processing_notes', JSONB, nullable=True)
        )


def downgrade() -> None:
    if column_exists('messaging_events', 'processing_notes'):
        op.drop_column('messaging_events', 'processing_notes')
