"""Add priority column to send_logs

Revision ID: i9j0k1l2_079_snd_prio
Revises: h8i9j0k1_078_fnl_sandbox
Create Date: 2026-03-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# revision identifiers, used by Alembic.
revision = 'i9j0k1l2_079_snd_prio'
down_revision = 'h8i9j0k1_078_fnl_sandbox'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # Add priority column
    if not column_exists('send_logs', 'priority'):
        op.add_column(
            'send_logs',
            sa.Column('priority', sa.Integer(), nullable=False, server_default=text("0")),
        )

    # Drop old deferred index
    if index_exists('ix_send_logs_deferred', 'send_logs'):
        op.drop_index('ix_send_logs_deferred', table_name='send_logs')

    # Create new composite index with priority ordering
    if not index_exists('ix_send_logs_deferred_prio', 'send_logs'):
        op.create_index(
            'ix_send_logs_deferred_prio',
            'send_logs',
            ['status', sa.text('priority DESC'), 'scheduled_at'],
            postgresql_where=text("status IN ('deferred', 'delayed')"),
        )


def downgrade() -> None:
    if index_exists('ix_send_logs_deferred_prio', 'send_logs'):
        op.drop_index('ix_send_logs_deferred_prio', table_name='send_logs')

    if not index_exists('ix_send_logs_deferred', 'send_logs'):
        op.create_index(
            'ix_send_logs_deferred',
            'send_logs',
            ['status', 'scheduled_at'],
            postgresql_where=text("status = 'deferred'"),
        )

    if column_exists('send_logs', 'priority'):
        op.drop_column('send_logs', 'priority')
